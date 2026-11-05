"""Single delivery-engine policy module (R1, mission ``РЕФАКТОРИНГ ДОСТАВКИ``).

This is the ONE place that decides whether a kanban event produces a user-visible
delivery, and the ONE place that renders + dispatches it across the lanes.

Decision contract (owner rules 1-5, REFACTOR-DESIGN.md §б):

  [live-status]   task entered work (running/spawned/claimed)  -> channel of record, edit-in-place
  [final]         the WHOLE branch rooted at the task closed     -> dual-lane (OWUI outbox + TG)
  [needs-decision] blocked(needs_input/capability/provider_route) | triage | block_loop_detected
                                                                -> dual-lane, immediately
  [aborted]       gave_up | timed_out | budget/limit | provider-failure | worker crashed
                                                                -> dual-lane
  (nothing)       everything else -> silenced + journaled (rule 3)

Only this module writes the suppression journal (``log_suppression``); every
other code path that used to journal a suppression decision has been deleted.

The engine is deliberately free of adapter knowledge: it consumes a small set
of injected async callbacks (``hooks``) supplied by ``kanban_watchers`` so it
inherits the existing cursor/outbox/wake mechanics without reimplementing them,
and can be unit-tested without a live gateway.

Idempotency:
  - [final]            branch_summary marker + fingerprint (existing, inherited)
  - [live-status]      delivery_live_status PK on task_id (restart-safe)
  - [needs-decision]   ``delivery_dedup`` persisted (task_id, kind, cause_hash)
  - [aborted]          same ``delivery_dedup`` shape
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


logger = logging.getLogger("gateway.delivery_engine")

# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

#: Valid engine event types (the only delivery families + the reopen status
#: update ping).
LIVE_STATUS = "live-status"
FINAL = "final"
NEEDS_DECISION = "needs-decision"
ABORTED = "aborted"
STATUS_UPDATE = "status-update"
EVENT_TYPES = (LIVE_STATUS, FINAL, NEEDS_DECISION, ABORTED, STATUS_UPDATE)

#: Notifier claim kinds the engine consumes.  Supersedes the legacy
#: ``TERMINAL_KINDS`` 11-kind list: adds the three live-status transitions
#: (running/spawned/claimed) and keeps the terminal/human-action kinds the
#: engine classifies structurally.  The cursor machinery (claim/rewind/
#: advance) is untouched — only the filter width changes, exactly as design
#: §в requires.
ENGINE_CLAIM_KINDS = frozenset({
    "completed",
    "blocked",
    "gave_up",
    "crashed",
    "timed_out",
    "status",
    "archived",
    "unblocked",
    "block_loop_detected",
    "review_requested",
    # live-status triggers (design §в: not claimed before R1)
    "spawned",
    "claimed",
})

#: ``blocked`` kinds that are genuinely human or platform-routing decisions.
_BLOCK_NEEDS_DECISION = frozenset({
    "needs_input", "capability", "provider_route",
})
#: ``blocked`` kinds that auto-resolve (never a needs-decision ping).
_BLOCK_SILENT = frozenset({"dependency", "transient"})

#: ``triage`` visible both as an event kind and as a card status.
_NEEDS_DECISION_KINDS = frozenset({
    "blocked", "needs_input", "capability", "block_loop_detected",
    "review_requested", "triage",
})

#: [aborted] event kinds -> render reason.  (crashed/timed_out auto-retry, so
#: they are NOT a creator wake under the design — but they ARE an aborted
#: delivery so the channel learns work stopped; wake stays off for them.)
_ABORTED_KINDS: Dict[str, str] = {
    "gave_up": "исчерпаны повторные сбои запуска",
    "timed_out": "превышен лимит времени выполнения",
    "crashed": "поток исполнителя прерван (будет повторён)",
}

#: Db ``delivery_dedup`` table (needs-decision/aborted once-only markers).
_DEDUP_TABLE = (
    "CREATE TABLE IF NOT EXISTS delivery_dedup ("
    " task_id TEXT NOT NULL,"
    " kind TEXT NOT NULL,"
    " cause_hash TEXT NOT NULL,"
    " created_at REAL NOT NULL,"
    " PRIMARY KEY (task_id, kind, cause_hash)"
    ")"
)

_LIVE_INTERVAL_S = 30        # first live re-render budget (design §2)
_LIVE_INTERVAL_CAP_S = 900   # 15 min soft cap
_LIVE_TITLE_MAX_CHARS = 80   # live-status title budget

#: Cap for the summary carried in the [final] итог (F1).  The card
#: body fixes "~2000 знаков" as the content budget for the terminal итог.
_OWUI_TG_SUMMARY_MAX_CHARS = 2000

#: Cap for root artifacts (F1).  Telegram's hard platform limit is 50 MB;
#: the contract fixes a comfortable 25 MB ceiling so an oversized artifact
#: is skipped with a warning instead of failing the whole delivery.
_OWUI_TG_ARTIFACT_MAX_BYTES = 25 * 1024 * 1024

#: Text extensions whose FIRST artifact of the root is both (a) attached as a
#: file AND (b) inlined as a summary when the event summary itself is empty —
#: the api_server-lane inline-report convention.
_OWUI_TG_INLINE_REPORT_EXTS = frozenset({".md", ".txt", ".rst", ".log"})
_OWUI_ARTIFACT_MAX_BYTES = 300_000
_OWUI_ARTIFACT_MAX_CHARS = 60_000

#: Report document formats eligible for the subtree deliverable fallback
#: (t_b154af7d).  HTML is the primary mission-report format here (Up Great
#: house style), PDF/DOCX are the usual external exports.
_REPORT_DOC_EXTS = frozenset({
    ".html", ".htm", ".pdf", ".docx", ".md", ".txt", ".rst",
})
#: At most this many subtree reports ride one terminal итог — the owner wants
#: the final report, not an archive dump.
_SUBTREE_REPORT_MAX_FILES = 3
#: Below this size a "report" is a stub/placeholder, not a deliverable.
_SUBTREE_REPORT_MIN_BYTES = 2_000

#: ``.../durable/<group>/<mission>`` — the mission durable dir a controller
#: card names as its "точка истины" (t_b154af7d fallback tier 2).
_MISSION_DIR_RE = re.compile(
    r"[A-Za-z]:[\\/][^\s\"'<>|]*?[\\/]durable[\\/][^\s\"'<>|/\\]+[\\/][^\s\"'<>|/\\]+"
)
#: Absolute document path inside a mission note (report candidates).
_ABS_DOC_RE = re.compile(
    r"[A-Za-z]:[\\/][^\s\"'<>|]+?\.(?:html|htm|pdf|docx)", re.IGNORECASE
)
#: Document formats a mission note may name as its final report.
_MISSION_REPORT_EXTS = frozenset({".html", ".htm", ".pdf", ".docx"})
#: A candidate file name must carry one of these tokens — this is what keeps
#: build scripts, datasets, opening plans and images out of the owner's chat.
_MISSION_REPORT_STEM_TOKENS = (
    "report", "otchet", "отчёт", "отчет", "digest", "дайджест",
)
#: Mission notes larger than this are logs/archives, not the plan/checkpoint.
_MISSION_NOTE_MAX_BYTES = 2 * 1024 * 1024
#: At most two declared reports (report + digest) ride one terminal итог.
_MISSION_REPORT_MAX_FILES = 2


# ---------------------------------------------------------------------------
# Render helpers (pure, no side effects)
# ---------------------------------------------------------------------------

def render_live_status(task: Any, *, assignee: str = "", title: str = "") -> str:
    """Render the short live-status line (rule 2, no LLM).

    ``task`` may be None (no row); ``assignee``/``title`` overrides let the
    watcher pass already-loaded values without a second DB read.
    """
    who = assignee or (getattr(task, "assignee", None) or "")
    ttl = title or (getattr(task, "title", None) or "")
    ttl = str(ttl).strip()[:_LIVE_TITLE_MAX_CHARS]
    who = str(who).strip()
    if who and ttl:
        return f"⚙ Выполняется: {who} — {ttl}"
    if who:
        return f"⚙ Выполняется: {who}"
    if ttl:
        return f"⚙ Выполняется — {ttl}"
    return "⚙ Выполняется…"


def render_needs_decision(task: Any, ev: Any) -> str:
    """Render the «⛔ Требуется решение» text (rule 4)."""
    ttl = (getattr(task, "title", None) if task else None) or ""
    ttl = str(ttl).strip()
    reason = ""
    payload = getattr(ev, "payload", None)
    if isinstance(payload, dict):
        r = payload.get("reason")
        if isinstance(r, str) and r.strip():
            reason = str(r).strip()
    # Human-action marker for the OWUI primary lane (R1 keeps the owner-facing
    # «Требуется решение» headline; the marker preserves the block semantics
    # the acceptance tests and operators read: a provider-route blocker shows
    # `[blocked]`, a triage / block_loop_detected card shows `[TRIAGE]`).
    ev_kind = getattr(ev, "kind", "") or ""
    card_status = str((getattr(task, "status", None) if task else None) or "").strip().lower()
    marker = ""
    if ev_kind == "block_loop_detected" or card_status == "triage":
        marker = "[TRIAGE] "
    elif ev_kind in ("blocked", "needs_input") and isinstance(payload, dict) and payload.get("provider_route"):
        marker = "[blocked] "
    head = f"⛔ Требуется решение: {marker}{ttl[:200]}" if ttl else f"⛔ Требуется решение {marker}".rstrip()
    if reason:
        # provider_route reasons may be long: keep the whole question
        # (the operator must see the alternatives).  Others cap at 160.
        cap = 500 if ev_kind == "blocked" and payload.get("provider_route") else 160
        head = f"{head}\n{reason[:cap]}"
    return head


def render_aborted(task: Any, ev: Any, *, reason: str = "") -> str:
    """Render the «✖ Прекращено без результата» text (rule 5)."""
    ttl = (getattr(task, "title", None) if task else None) or ""
    ttl = str(ttl).strip()
    if not reason:
        kind = getattr(ev, "kind", "") or ""
        reason = _ABORTED_KINDS.get(kind, "")
    if not reason:
        payload = getattr(ev, "payload", None)
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, str) and err.strip():
                reason = str(err).strip()[:200]
    head = f"✖ Прекращено без результата: {ttl[:200]}" if ttl else "✖ Прекращено без результата"
    if reason:
        head = f"{head}\n{reason}"
    return head


def render_final(msg: str, plan: Optional[dict] = None,
                 summary: str = "", artifacts: Optional[list] = None) -> str:
    """Render the [final] итог text.

    ``msg`` is the baked branch one-liner (``✅ Задача выполнена`` / ``❌ …``);
    when ``summary``/``artifacts`` are present they are appended — the composite
    is what the dual lane delivers, so a bare итог never rides through on its
    own (F1 convention preserved inside the engine render, not as a side gate).
    """
    h = (msg or "").strip() or "✅ Задача выполнена"
    parts: List[str] = [h]
    s = (summary or "").strip()
    if s:
        # Render-zone dedup: the composed summary often already carries the
        # identical baked header (owui_root_final_summary / the F1 итог), so
        # appending it verbatim would double the ✅ header.  Skip the first
        # occurrence and keep the body — final output stays `✅ …\n\ndone`,
        # never `✅ …\n\n✅ …\n\ndone`.
        if s.startswith(h):
            s = s[len(h):].strip("\n")
        if s:
            parts.append(s)
    if artifacts:
        ok = [a for a in artifacts if isinstance(a, str) and a.strip()]
        if ok:
            parts.append("📎 " + ", ".join(ok))
    return "\n\n".join(parts).strip()


# ---------------------------------------------------------------------------
# [final] content helpers (F1, tg-delivery-contract)
#
# The branch итог must carry CONTENT: a short summary from the root's
# completed event / kanban_complete.summary plus the root's artifacts attached
# as files.  A bare one-liner with no summary and no artifacts (the "голый
# итог") is never delivered as a contentless ping.  These helpers moved here
# verbatim from kanban_watchers (R1 legacy dismantle) — the engine is the
# single place that renders [final].
# ---------------------------------------------------------------------------

def owui_root_final_summary(plan: dict, msg: str,
                            *,
                            inline_report: bool = True) -> str:
    """Full итог text for the [final] branch mirror (F1, tg-delivery-contract).

    ``msg`` is the short baked one-liner (``✅ Задача выполнена`` or the ❌
    list).  A real итог carries CONTENT: a short summary (~2000 chars) from the
    root's ``completed`` event (kanban_complete.summary) plus the root's
    artifacts.  Composes the best available итог:

      1. The root's last ``completed`` event payload (first summary line is
         already capped at 400 chars by kanban_db.complete_task; the run row
         holds the full text — we take the short summary up to
         ``_OWUI_TG_SUMMARY_MAX_CHARS`` from the run row, falling back to the
         event's ``summary`` field).
      2. If that summary is empty, the root's first inline text report
         artifact (md/txt/rst/log) is inlined as a report.
      3. The original short ``msg`` is kept only when neither produces real
         content, so the mirror never fabricates text the worker didn't write.

    Pure and side-effect free apart from a closed DB read; failures degrade to
    ``msg`` so the delivery pipeline never breaks on data access.
    """
    from hermes_cli import kanban_db as _kb
    import json as _json

    root_id = plan.get("root_id")
    if not root_id:
        return msg
    try:
        conn = _kb.connect(board=plan.get("board"))
        try:
            # Full summary lives on the closing run row (first 400 chars of
            # the summary also ride the event payload).  The run row carries
            # the worker's full handoff; the event payload is the durable
            # fallback when the run row was synthesized/short.
            run = conn.execute(
                "SELECT summary, metadata FROM task_runs "
                "WHERE task_id=? AND outcome='completed' "
                "ORDER BY COALESCE(ended_at, started_at, 0) DESC, id DESC LIMIT 1",
                (root_id,),
            ).fetchone()
            payload = None
            if run is not None and run["summary"]:
                summary_text = str(run["summary"]).strip()
            else:
                row = conn.execute(
                    "SELECT payload FROM task_events WHERE task_id=? "
                    "AND kind='completed' ORDER BY id DESC LIMIT 1",
                    (root_id,),
                ).fetchone()
                if row and row[0]:
                    payload = _json.loads(row[0])
                summary_text = ""
                if isinstance(payload, dict):
                    s = payload.get("summary")
                    if isinstance(s, str):
                        summary_text = s.strip()
            artifacts: list[str] = []
            if isinstance(payload, dict):
                raw = payload.get("artifacts")
                if isinstance(raw, (list, tuple)):
                    artifacts = [str(p) for p in raw if isinstance(p, str) and p.strip()]
            # metadata["artifacts"] may also carry paths the run row promoted
            # (complete_task merges prose artifacts into metadata before the
            # payload is built) — prefer the event payload list, then the run
            # metadata as an extra source.
            run_artifacts: list[str] = []
            if run is not None:
                try:
                    m = _json.loads(run["metadata"]) if run["metadata"] else {}
                except (TypeError, ValueError):
                    m = {}
                if isinstance(m, dict):
                    rawm = m.get("artifacts")
                    if isinstance(rawm, (list, tuple)):
                        run_artifacts = [str(p) for p in rawm if isinstance(p, str) and p.strip()]
            for p in run_artifacts:
                if p not in artifacts:
                    artifacts.append(p)
        finally:
            conn.close()
    except Exception:
        return msg

    parts: list[str] = []
    # Header: keep the short baked итог line (``✅ Задача выполнена`` /
    # ``❌ …нужен фикс``) so the message still reads as "the branch closed"
    # even when the handoff summary is short; the summary rides below.
    parts.append(msg.strip() or "✅ Задача выполнена")
    if summary_text:
        parts.append(summary_text[:_OWUI_TG_SUMMARY_MAX_CHARS])
    # F1/terminal-итог contract: the итог text carries the handoff summary;
    # artifact FILES ride ``deliver_artifacts`` (send_document) — never
    # inlined into the text, so a report is uploaded as a native file on both
    # lanes instead of duplicating its content into the message body.
    #
    # OWUI-stage-only exception (``inline_report=True``): api_server has no
    # attachment channel, so when the root attached a report file that text
    # must ride inside the staged chat message itself (R1 convention) — the
    # OWUI stage is the sole consumer of the inline form.
    if inline_report:
        from hermes_cli.delivery_policy import sanitize_delivery_text as _sane_inl
        for raw in artifacts:
            try:
                p = Path(raw)
                if not p.is_file() or p.suffix.lower() not in _OWUI_TG_INLINE_REPORT_EXTS:
                    continue
                if p.stat().st_size > _OWUI_ARTIFACT_MAX_BYTES:
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")[:_OWUI_ARTIFACT_MAX_CHARS]
                text = _sane_inl(text)
                if text:
                    parts.append(f"📄 Отчёт <b>{p.name}</b>:\n\n{text}")
                    break
            except (OSError, ValueError):
                continue
    full = "\n\n".join(parts).strip()
    if not full or full == (msg or "").strip():
        return msg
    from hermes_cli.delivery_policy import sanitize_delivery_text as _sane
    return _sane(full, max_chars=8000)


def _is_final_report_name(stem: str) -> bool:
    """True when a file stem names a MISSION-LEVEL report, not a stage note.

    ``sno-economics-report-for-manager-v8`` / ``digests/day-01`` → True;
    ``S1_REPORT`` / ``S1D_REPORT`` / ``FINAL-V8-ACCEPTANCE`` → False.  Stage
    deliverables carry a leading stage token (``s1``, ``s2b``, ``d3``), which
    this rejects so the owner gets the final mission report rather than the
    per-stage paper trail.
    """
    s = (stem or "").lower()
    if not s:
        return False
    if not any(tok in s for tok in _MISSION_REPORT_STEM_TOKENS):
        return False
    # Leading stage id: s1, s1d, s2-b, d1, w9a, m2 … — a per-stage artifact.
    if re.match(r"^(?:s|d|w|m|a|b)\d+[a-z]?(?:[-_.]|$)", s):
        return False
    return True


def _report_family_key(path: Path) -> str:
    """Normalized report family key — collapses superseded versions.

    ``sno-economics-report-for-manager-v7.html`` and ``...-v8.html`` are the
    SAME deliverable at two revisions; shipping both to the owner is noise and
    looks like two reports.  Version markers (``-v7``, ``_v8.1``, ``-r3``,
    ``-final``, ``-draft``) are stripped so only the newest member of each
    family survives.
    """
    stem = path.stem.lower()
    stem = re.sub(
        r"(?:[-_.]?(?:v|ver|version)\d+(?:[._]\d+)*"
        r"|[-_.]?r\d+"
        r"|[-_.](?:final|draft|ready|copy|new|old)\b"
        r"|[-_.]?\d{8}(?:[-_]\d{6})?)$",
        "",
        stem,
    )
    return re.sub(r"[-_.]{2,}", "-", stem).strip("-_.")


def _dedupe_report_families(scored: list[tuple[float, int, str]]) -> list[str]:
    """Keep one (newest, then largest) file per report family.

    ``scored`` is ``[(mtime, size, path), ...]``; the returned list preserves
    the input order and drops every older member of an already-taken family.
    """
    taken: set[str] = set()
    out: list[str] = []
    for _mtime, _size, path in scored:
        try:
            key = _report_family_key(Path(path))
        except Exception:
            key = path
        if key in taken:
            logger.info(
                "delivery engine: [final] %s superseded by a newer revision of "
                "the same report — not delivered", path,
            )
            continue
        taken.add(key)
        out.append(path)
    return out


def _subtree_report_paths(conn: Any, root_id: str) -> list[str]:
    """Report-like artifact paths produced by a root's subtree (t_b154af7d).

    Mission controllers are routinely closed by an administrative archive
    rather than ``kanban_complete(artifacts=...)``, so the root carries no
    artifact of its own while the actual deliverable was produced by a
    descendant card.  Without this fallback the terminal итог degenerates to
    a bare one-liner and the owner never receives the report (the exact
    sno-economics-r3 incident: controller t_08a192ec had 0 artifacts, the
    report file was produced under S4/S6).

    Selection rules (deliberately narrow — a wrong file in the owner's chat
    is worse than none):

    * only files whose extension is a report format (``_REPORT_DOC_EXTS``);
    * only files that exist on disk, are absolute and readable;
    * newest first by mtime, at most ``_SUBTREE_REPORT_MAX_FILES``;
    * duplicate content (same name+sha256) collapses to the first hit;
    * a candidate must be at least ``_SUBTREE_REPORT_MIN_BYTES`` (skips
      stubs/placeholders) and at most the 25 MB attachment ceiling.

    Returns ``[]`` on any failure — the caller degrades to the text итог.
    """
    paths: list[str] = []
    try:
        from hermes_cli import kanban_db as _kb
        import json as _json

        node_ids = {root_id} | set(_kb.get_descendant_ids(conn, root_id) or ())
        if len(node_ids) < 2:
            return []
        placeholders = ",".join("?" * len(node_ids))
        candidates: list[str] = []
        rows = conn.execute(
            "SELECT task_id, payload FROM task_events "
            f"WHERE task_id IN ({placeholders}) AND kind='completed' "
            "ORDER BY id DESC",
            tuple(sorted(node_ids)),
        ).fetchall()
        for row in rows:
            payload = row["payload"] if not isinstance(row, tuple) else row[1]
            if not payload:
                continue
            try:
                data = _json.loads(payload)
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict):
                for p in data.get("artifacts") or ():
                    if isinstance(p, str) and p.strip():
                        candidates.append(p.strip())
        runs = conn.execute(
            "SELECT metadata FROM task_runs "
            f"WHERE task_id IN ({placeholders}) AND outcome='completed' "
            "ORDER BY COALESCE(ended_at, started_at, 0) DESC, id DESC",
            tuple(sorted(node_ids)),
        ).fetchall()
        for row in runs:
            meta = row["metadata"] if not isinstance(row, tuple) else row[0]
            if not meta:
                continue
            try:
                m = _json.loads(meta)
            except (TypeError, ValueError):
                continue
            if isinstance(m, dict):
                for p in m.get("artifacts") or ():
                    if isinstance(p, str) and p.strip():
                        candidates.append(p.strip())
        for tid in sorted(node_ids):
            try:
                for att in _kb.list_attachments(conn, tid):
                    if att.stored_path:
                        candidates.append(str(att.stored_path))
            except Exception:
                continue
    except Exception as exc:
        logger.warning(
            "delivery engine: subtree report scan for %s failed: %s",
            root_id, exc,
        )
        return []

    scored: list[tuple[float, int, str]] = []
    seen_fp: set[tuple[str, str]] = set()
    for raw in candidates:
        try:
            p = Path(os.path.expanduser(raw))
            if not p.is_absolute() or not p.is_file():
                continue
            if p.suffix.lower() not in _REPORT_DOC_EXTS:
                continue
            size = p.stat().st_size
            if size < _SUBTREE_REPORT_MIN_BYTES or size > _OWUI_TG_ARTIFACT_MAX_BYTES:
                continue
            digest = hashlib.sha256()
            with open(p, "rb") as fh:
                while chunk := fh.read(1024 * 1024):
                    digest.update(chunk)
            fp = (p.name, digest.hexdigest())
            if fp in seen_fp:
                continue
            seen_fp.add(fp)
            scored.append((p.stat().st_mtime, size, str(p)))
        except (OSError, ValueError):
            continue
    scored.sort(reverse=True)
    deduped = _dedupe_report_families(scored)
    # Prefer the FINAL report over stage/auxiliary deliverables: when the
    # subtree yields any file whose name reads as a report/digest, ship only
    # those and drop stage notes (``S1_REPORT.md``) and acceptance memos — the
    # owner asked for the mission's final report, not the paper trail.  When
    # nothing is report-named, the explicit artifacts are shipped as-is.
    final_like = [
        p for p in deduped
        if _is_final_report_name(Path(p).stem.lower())
    ]
    chosen = final_like or deduped
    for path in chosen[:_SUBTREE_REPORT_MAX_FILES]:
        if path not in paths:
            paths.append(path)
    return paths


def _mission_declared_reports(conn: Any, root_id: str) -> list[str]:
    """Final-report files EXPLICITLY declared by a mission card (t_b154af7d).

    Second fallback tier, for the common case where a mission controller was
    archived administratively: the root carries no artifacts, its subtree
    carries none either, yet the mission's own "точка истины" documents name
    the final report by absolute path (sno-economics-r3: NIGHT-PLAN.md /
    checkpoint.md declare
    ``C:/Users/hermes/vleo_economics/report/manager/sno-economics-report*.html``).

    Resolution is evidence-based, never a directory guess:

    1. the root card's own text (body, result, comments) must name a mission
       durable directory (``.../durable/<group>/<mission>``);
    2. only ``*.md`` files at the top of THAT directory are read (the mission's
       plan/checkpoint/handoff notes);
    3. a candidate must be an absolute path to an EXISTING document file
       (html/htm/pdf/docx) whose stem names a report (report / отчёт / otchet /
       digest / дайджест) — plan scripts, datasets and images never qualify;
    4. duplicates collapse, newest first, capped at 2.

    Returns ``[]`` on any failure; the caller then degrades to the text итог.
    """
    try:
        import json as _json
        from hermes_cli import kanban_db as _kb

        task = _kb.get_task(conn, root_id)
        if task is None:
            return []
        chunks: list[str] = [
            str(getattr(task, "title", "") or ""),
            str(getattr(task, "body", "") or ""),
            str(getattr(task, "result", "") or ""),
        ]
        try:
            for c in _kb.list_comments(conn, root_id)[-40:]:
                chunks.append(str(getattr(c, "body", "") or ""))
        except Exception:
            pass
        card_text = "\n".join(chunks)
        if not card_text.strip():
            return []
        dirs = set()
        for m in _MISSION_DIR_RE.finditer(card_text):
            try:
                cand = Path(m.group(0).replace("\\", "/").rstrip("/.,;)"))
                if cand.is_dir():
                    dirs.add(cand)
            except (OSError, ValueError):
                continue
        if not dirs:
            return []

        declared: list[str] = []
        for mission_dir in sorted(dirs):
            try:
                note_files = sorted(
                    (p for p in mission_dir.glob("*.md") if p.is_file()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )[:8]
            except OSError:
                continue
            for note in note_files:
                try:
                    if note.stat().st_size > _MISSION_NOTE_MAX_BYTES:
                        continue
                    text = note.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for raw in _ABS_DOC_RE.findall(text):
                    norm = raw.replace("\\", "/").rstrip(".,;)]»\"'")
                    try:
                        p = Path(norm)
                    except (OSError, ValueError):
                        continue
                    if not p.is_absolute() or not p.is_file():
                        continue
                    if p.suffix.lower() not in _MISSION_REPORT_EXTS:
                        continue
                    stem = p.stem.lower()
                    if not any(tok in stem for tok in _MISSION_REPORT_STEM_TOKENS):
                        continue
                    try:
                        size = p.stat().st_size
                    except OSError:
                        continue
                    if size < _SUBTREE_REPORT_MIN_BYTES or size > _OWUI_TG_ARTIFACT_MAX_BYTES:
                        continue
                    if norm not in declared:
                        declared.append(norm)
        if not declared:
            return []
        scored_mission: list[tuple[float, int, str]] = []
        for path in declared:
            try:
                st = Path(path).stat()
            except OSError:
                continue
            scored_mission.append((st.st_mtime, st.st_size, path))
        scored_mission.sort(reverse=True)
        return _dedupe_report_families(scored_mission)[:_MISSION_REPORT_MAX_FILES]
    except Exception as exc:
        logger.warning(
            "delivery engine: mission declared-report scan for %s failed: %s",
            root_id, exc,
        )
        return []


def owui_root_artifact_paths(plan: dict) -> list[str]:
    """Absolute readable artifact paths for the [final] branch итог (F1).

    Sources, in priority order:
      1. ``artifacts[]`` on the root's last ``completed`` event payload
         (kanban_complete(artifacts=...) promoted by complete_task).
      2. ``metadata["artifacts"]`` on the root's closing run row.
      3. ``task_attachments`` rows for the root (files attached via
         kanban_attach / kanban_attach_url).

    Every candidate is verified to exist on disk; missing files are dropped
    with a warning (missing paths never drop delivery of the text).
    """
    from hermes_cli import kanban_db as _kb
    import json as _json

    root_id = plan.get("root_id")
    if not root_id:
        return []
    paths: list[str] = []
    seen: set[str] = set()

    def _add(raw) -> None:
        if not isinstance(raw, str) or not raw.strip():
            if not isinstance(raw, str):
                logger.warning(
                    "delivery engine: [final] artifact entry for root "
                    "%s is non-string (%s) — skipped",
                    root_id, repr(raw)[:200],
                )
            return
        try:
            check = Path(os.path.expanduser(raw.strip()))
        except (OSError, ValueError):
            logger.warning(
                "delivery engine: [final] artifact %r for root %s has "
                "an invalid path — skipped",
                raw.strip(), root_id,
            )
            return
        if not check.is_absolute():
            logger.warning(
                "delivery engine: [final] artifact %r for root %s is "
                "not an absolute path — skipped",
                raw.strip(), root_id,
            )
            return
        resolved = str(check.resolve(strict=False))
        if resolved in seen:
            return
        seen.add(resolved)
        if not check.is_file():
            logger.warning(
                "delivery engine: [final] artifact %s for root %s "
                "missing on disk — skipped",
                raw.strip(), root_id,
            )
            return
        try:
            if check.stat().st_size > _OWUI_TG_ARTIFACT_MAX_BYTES:
                logger.warning(
                    "delivery engine: [final] artifact %s for root %s "
                    "exceeds 25MB cap — skipped",
                    raw.strip(), root_id,
                )
                return
        except OSError:
            pass
        paths.append(resolved)

    try:
        conn = _kb.connect(board=plan.get("board"))
        try:
            row = conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? "
                "AND kind='completed' ORDER BY id DESC LIMIT 1",
                (root_id,),
            ).fetchone()
            if row and row[0]:
                try:
                    payload = _json.loads(row[0])
                except (TypeError, ValueError):
                    payload = None
                if isinstance(payload, dict):
                    for p in payload.get("artifacts") or ():
                        _add(p)
            run = conn.execute(
                "SELECT metadata FROM task_runs "
                "WHERE task_id=? AND outcome='completed' "
                "ORDER BY COALESCE(ended_at, started_at, 0) DESC, id DESC LIMIT 1",
                (root_id,),
            ).fetchone()
            if run is not None and run["metadata"]:
                try:
                    m = _json.loads(run["metadata"])
                except (TypeError, ValueError):
                    m = None
                if isinstance(m, dict):
                    for p in m.get("artifacts") or ():
                        _add(p)
            # task_attachments rows — durable per-task attachment blobs.
            for att in _kb.list_attachments(conn, root_id):
                _add(att.stored_path)
            # 4. Mission-report fallback (t_b154af7d): a mission controller is
            #    frequently closed by an administrative archive (owner order)
            #    WITHOUT a ``completed`` event and without attaching anything —
            #    the real deliverable then lives only as a file on disk that a
            #    DESCENDANT card produced (sno-economics-r3: the controller
            #    t_08a192ec had zero artifacts while the report was produced by
            #    S4/S6 cards under a different root).  When the root itself
            #    yields nothing, fall back to the report-like artifacts of its
            #    own subtree so the owner still receives the final report
            #    instead of a bare "✅ Задача выполнена".
            if not paths:
                for cand in _subtree_report_paths(conn, root_id):
                    _add(cand)
            # 5. Mission-declared report (t_b154af7d): the mission's own
            #    "точка истины" notes name the final report by absolute path.
            #    This covers an administratively archived controller whose
            #    subtree cards produced the report outside the board (the
            #    sno-economics-r3 case).  Strictly evidence-based: only files
            #    actually named in the mission's md notes, existing on disk.
            if not paths:
                for cand in _mission_declared_reports(conn, root_id):
                    _add(cand)
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(
            "delivery engine: could not enumerate root artifacts for "
            "%s: %s", root_id, exc,
        )
        return paths
    return paths


# ---------------------------------------------------------------------------
# Dedup markers (persisted, restart-safe)
# ---------------------------------------------------------------------------

def _cause_hash(ev: Any, kind: str) -> str:
    """Stable hash of the transition cause for dedup.

    For blocked/needs_input events the cause signature is the NORMALIZED FIRST
    NON-EMPTY LINE of the reason (``_tg_block_mirror_cause_sig`` semantics): a
    re-wrapped or tail-extended version of the same human cause still dedups,
    and two blocks sharing a first line are treated as the same cause (D3) —
    the worth-of-attention question is the first line the owner reads.  For
    aborted the payload error; everything else hashes the payload verbatim.
    Same cause after a real delivery is suppressed (drift_alerted-style
    once-only, design §2b).
    """
    from hermes_cli.kanban_db import _tg_block_mirror_cause_sig as _cause_sig
    payload = getattr(ev, "payload", None)
    ev_kind = getattr(ev, "kind", "") or ""
    raw: Any = ""
    if ev_kind in ("blocked", "needs_input"):
        reason = ""
        if isinstance(payload, dict):
            reason = str(payload.get("reason") or "")
        return hashlib.sha256(
            f"{ev_kind}|{kind}|block|{_cause_sig(reason)}".encode("utf-8")
        ).hexdigest()[:24]
    if isinstance(payload, dict):
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, default=str,
        )
    return hashlib.sha256(f"{ev_kind}|{kind}|{raw}".encode("utf-8")).hexdigest()[:24]


def ensure_dedup_table(conn: Any) -> None:
    """Idempotently create the persisted dedup table (call on engine init)."""
    try:
        conn.execute(_DEDUP_TABLE)
        conn.commit()
    except Exception as exc:
        logger.debug("delivery engine: dedup table ensure failed: %s", exc)


def dedup_seen(conn: Any, task_id: str, kind: str, cause_hash: str) -> bool:
    """True when (task_id, kind, cause_hash) was already delivered."""
    try:
        row = conn.execute(
            "SELECT 1 FROM delivery_dedup WHERE task_id=? AND kind=? AND cause_hash=?",
            (task_id, kind, cause_hash),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def dedup_mark(conn: Any, task_id: str, kind: str, cause_hash: str) -> None:
    """Persist a delivered dedup marker (best-effort, never blocks delivery)."""
    try:
        conn.execute(
            "INSERT OR IGNORE INTO delivery_dedup "
            "(task_id, kind, cause_hash, created_at) VALUES (?,?,?,?)",
            (task_id, kind, cause_hash, time.time()),
        )
        conn.commit()
    except Exception as exc:
        logger.debug("delivery engine: dedup mark failed: %s", exc)


# ---------------------------------------------------------------------------
# Suppression journal — the ONE place that writes it
# ---------------------------------------------------------------------------

def log_suppression(
    app: str,
    who: str,
    target: str,
    decision: Any,
    snippet: str = "",
    extra: Optional[dict] = None,
) -> None:
    """Write a suppression record to the standard journal (single point).

    Delegates to ``hermes_cli.delivery_policy.log_suppression`` (kept there
    as the uniform journal writer); never raises.
    """
    try:
        from hermes_cli.delivery_policy import log_suppression as _journal

        _journal(
            app=app,
            who=who or "",
            target=target or "",
            decision=decision,
            snippet=snippet or "",
            extra=extra or None,
        )
    except Exception as exc:
        logger.debug("delivery engine: suppression journal failed: %s", exc)


# ---------------------------------------------------------------------------
# Report-delivery journal — the ONE place that records «отчёт отправлен»
# ---------------------------------------------------------------------------

#: Durable JSON-lines journal for the owner's Telegram report deliveries.
#: Separate from the rotating gateway.log so the audit trail of «финальный
#: отчёт доставлен» survives log rotation (t_e04a8f44 acceptance: «в логах
#: видно событие отправки без ручного действия»).
_REPORT_DELIVERY_LOG_NAME = "telegram-report-delivery.log"

#: Report type buckets keyed by file extension.
_REPORT_TYPE_BY_EXT = {
    ".html": "html",
    ".htm": "html",
    ".pdf": "pdf",
    ".md": "markdown",
    ".txt": "text",
    ".rst": "text",
    ".log": "text",
    ".csv": "data",
    ".json": "data",
    ".xlsx": "spreadsheet",
    ".docx": "document",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
}


def mask_chat_id(chat_id: Any) -> str:
    """Mask a delivery chat id for logs (owner PII must stay out of them).

    Numeric Telegram ids keep only the last 3 digits (``125805788`` →
    ``******788``); non-numeric ids keep a short stable prefix so a log reader
    can tell sessions apart without the full identifier.
    """
    raw = str(chat_id or "").strip()
    if not raw:
        return "?"
    if raw.isdigit():
        keep = raw[-3:] if len(raw) > 3 else ""
        return ("*" * (len(raw) - len(keep))) + keep
    head = raw.split("-")[0][:12]
    return "{}…".format(head)


def report_type_of(paths: Any) -> str:
    """Return a coarse report type string for a list of artifact paths."""
    if not paths:
        return "none"
    if isinstance(paths, str):
        paths = [paths]
    kinds: List[str] = []
    for p in paths:
        ext = Path(str(p)).suffix.lower()
        kind = _REPORT_TYPE_BY_EXT.get(ext, "file")
        if kind not in kinds:
            kinds.append(kind)
    return "+".join(kinds) if kinds else "none"


def log_report_delivery(
    *,
    task_id: str,
    chat_id: Any,
    result: str,
    platform: str = "telegram",
    reason: str = "",
    report_type: str = "none",
    artifacts: Optional[list] = None,
    artifacts_failed: Optional[list] = None,
    text_len: int = 0,
    trigger: str = "branch_terminal",
    board: str = "",
    artifacts_skipped: Optional[list] = None,
    artifacts_discovered: Optional[list] = None,
    extra: Optional[dict] = None,
) -> None:
    """Record ONE «финальный отчёт → владелец» delivery event.

    Emits BOTH a single-line INFO/WARNING record on the standard logger (so
    it shows up next to the rest of the gateway activity) and a JSON-lines
    record in ``<hermes_home>/logs/telegram-report-delivery.log`` (durable
    audit trail).  Fields covered — card/mission, masked chat id, report
    type, result, artifacts, time, trigger.

    Never raises: a journal write must not break the delivery path.
    """
    art = [str(p) for p in (artifacts or []) if p]
    failed = [str(p) for p in (artifacts_failed or []) if p]
    skipped = [str(p) for p in (artifacts_skipped or []) if p]
    found = [str(p) for p in (artifacts_discovered or []) if p]
    masked = mask_chat_id(chat_id)
    tail = ""
    if failed:
        tail += " artifacts_failed=%d" % len(failed)
    if skipped:
        tail += " artifacts_skipped=%d" % len(skipped)
    if found:
        # Found-on-disk report files that never left the host (a failed text
        # send aborts the lane before any upload) — distinct from ``artifacts``
        # (uploaded) and from ``artifacts_failed`` (upload attempted + failed).
        tail += " artifacts_not_uploaded=%d" % len(found)
    if reason:
        tail += " reason=%s" % reason
    line = (
        "telegram report delivery: task={task} chat={chat} report={rtype} "
        "result={result} artifacts={n} trigger={trigger}{tail}"
    ).format(
        task=task_id,
        chat=masked,
        rtype=report_type,
        result=result,
        n=len(art),
        trigger=trigger,
        tail=tail,
    )
    try:
        if result == "delivered":
            logger.info(line)
        else:
            logger.warning(line)
    except Exception:
        pass
    try:
        from datetime import datetime, timezone

        from hermes_constants import get_hermes_home

        now = datetime.now(timezone.utc)
        record: Dict[str, Any] = {
            "ts": now.isoformat(timespec="seconds"),
            "ts_epoch": round(now.timestamp(), 3),
            "event": "report_delivery",
            "trigger": trigger,
            "board": board or "",
            "task_id": task_id or "",
            "platform": platform or "telegram",
            "chat_id_masked": masked,
            "report_type": report_type,
            "result": result,
            "reason": reason or "",
            "text_len": int(text_len or 0),
            "artifacts": art,
            "artifacts_failed": failed,
            "artifacts_skipped": skipped,
            "artifacts_not_uploaded": found,
            "manual": False,
        }
        if extra:
            record.update(extra)
        override = (os.environ.get("HERMES_REPORT_DELIVERY_LOG") or "").strip()
        if override:
            log_path = Path(override)
        else:
            log_path = get_hermes_home() / "logs" / _REPORT_DELIVERY_LOG_NAME
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug("delivery engine: report-delivery journal failed: %s", exc)



# ---------------------------------------------------------------------------
# Event classification — the ONLY decision point
# ---------------------------------------------------------------------------

@dataclass
class EngineDecision:
    """A classified event the engine will render + dispatch (or silence)."""
    event_type: Optional[str]      # one of EVENT_TYPES, or None = silence
    reason: str = ""
    detail: str = ""


def classify_event(
    task: Any,
    ev: Any,
    tree_ctx: Optional[dict] = None,
    *,
    silent: bool = False,
    do_not_deliver: bool = False,
) -> EngineDecision:
    """Return the delivery decision for ONE (task, event, tree context).

    This is the single classification authority.  Everything that does not
    map to one of the four event types returns ``event_type=None``
    (silenced + journaled by the caller).

    ``tree_ctx`` (provided by the watcher collection pass)::
        - branch_terminal: bool  — the WHOLE branch rooted at the task closed
        - card_status: str        — current task status (triage detection)
        - is_root: bool           — no subscribed ancestor
        - mission_live: bool      — live goal-mode controller in the session
        - block_kind: str|None    — tasks.block_kind for the card
    """
    if do_not_deliver:
        return EngineDecision(None, "do_not_deliver", "synthetic readback card")
    if silent:
        # Descendant of a subscribed root: the branch итог owns the single
        # message — but human-action needs-decision + aborted still classify
        # (they are the exceptions the old code kept loud).
        pass

    ctx = tree_ctx or {}
    kind = getattr(ev, "kind", None) or ""
    card_status = str(ctx.get("card_status") or "").strip().lower()

    # ---- [live-status] -------------------------------------------------
    is_running = False
    if kind in ("spawned", "claimed"):
        is_running = True
    elif kind == "status":
        _pl = getattr(ev, "payload", None)
        if isinstance(_pl, dict):
            is_running = str(_pl.get("status") or _pl.get("to") or "").lower() == "running"
    if is_running:
        return EngineDecision(LIVE_STATUS, "live-status", f"kind={kind}")

    # ---- [needs-decision] ---------------------------------------------
    # Design §в line 168: ``triage`` is signalled BOTH as an event kind AND
    # as the card's current status (``tasks.status = 'triage'``).  A card in
    # triage needs a human decision no matter which claimed event carries the
    # notification (e.g. ``block_loop_detected``, or a re-`status` while the
    # card sits in triage) — the legacy OWUI predicate kept triage cards loud
    # (watchers: card_status == "triage" -> not suppressed) and the engine
    # must preserve that.
    if card_status == "triage":
        return EngineDecision(NEEDS_DECISION, "card-status triage",
                              "tasks.status=triage (human decision)")
    if kind in _NEEDS_DECISION_KINDS:
        # Prefer the block kind recorded ON the event (the reason the block
        # happened) over the task's CURRENT block_kind: a later transition
        # (e.g. ``completed``) clears tasks.block_kind to NULL, but a claimed
        # ``blocked`` event still describes its own kind.  A self-resolving
        # block (dependency/transient) must stay silent regardless of what the
        # card looks like now; a human-action block stays loud.
        pk = ctx.get("block_kind") or ""
        if kind in ("blocked", "needs_input"):
            ev_payload = getattr(ev, "payload", None)
            ev_kind = (
                str((ev_payload or {}).get("kind") or "").strip().lower()
                if isinstance(ev_payload, dict) else ""
            )
            block_kind = ev_kind or pk or ""
            if block_kind in _BLOCK_SILENT:
                return EngineDecision(
                    None, "auto-resolve block", f"block_kind={block_kind or '?'}",
                )
            if block_kind in _BLOCK_NEEDS_DECISION or not block_kind:
                # un-typed block == human decision (branch_state semantics)
                return EngineDecision(NEEDS_DECISION, "blocked", f"block_kind={block_kind}")
            return EngineDecision(None, "auto-resolve block", f"block_kind={block_kind}")
        # capability / block_loop_detected / review_requested / triage
        return EngineDecision(NEEDS_DECISION, kind, f"event_kind={kind}")

    # ---- [aborted] -----------------------------------------------------
    if kind in _ABORTED_KINDS:
        return EngineDecision(ABORTED, kind, f"event_kind={kind}")

    # ---- [final] -------------------------------------------------------
    if kind == "completed":
        if ctx.get("branch_terminal"):
            return EngineDecision(FINAL, "branch terminal", "whole branch closed")
        return EngineDecision(
            None, "intermediate close", "branch not terminal (rule 3)",
        )

    # ---- everything else: silence -------------------------------------
    return EngineDecision(None, "not an engine event", f"event_kind={kind}")


# ---------------------------------------------------------------------------
# Dispatch — one table of lanes, no parallel policy decisions
# ---------------------------------------------------------------------------

@dataclass
class DeliveryHooks:
    """Async callbacks the watcher injects (kept out of the policy core)."""
    stage_owui: Callable[..., Any]                          # api_server outbox
    send_tg: Callable[..., Any]                             # adapter.send
    edit_tg: Callable[..., Any]                             # adapter.edit_message
    wake: Callable[..., Any]                                # deliver_wake
    on_advance: Callable[..., Any]                          # cursor advance
    on_rewind: Callable[..., Any]                           # cursor rewind
    resolve_tg_adapter: Callable[..., Any]                   # -> adapter|None


@dataclass
class LaneCtx:
    """One lane resolution for a subscription."""
    platform: str
    chat_id: str
    thread_id: str = ""
    adapter: Optional[Any] = None
    #: resolved bound TG chat for api_server -> owner; None = no TG lane
    tg_chat: Optional[str] = None
    tg_allowed: bool = False


def resolve_author_telegram(
    task: Any,
    sub: Optional[dict] = None,
    owui_to_tg: Optional[dict] = None,
) -> Optional[str]:
    """Resolve the Telegram chat bound to a task's author/owner (OWUI→TG).

    ЕДИНСТВЕННАЯ точка маппинга «автор/владелец канала постановки
    (api_server-подписка) → его Telegram» (карточка 1, AUDIT-PLAN
    §5; критерий 4 — не дублируем OWUI-цепочку). Источник прав —
    серверная привязка ``owui_to_tg`` (delivery_policy.owui_telegram_users(),
    T2 §8.1), которая инжектится в движок watcher'ом
    (kanban_watchers.py:1202-1206). Все вызовы (включая legacy
    ``_tg_bound_chat_for``) идут через этот хелпер.

    Автор = владелец канала постановки = ``kanban_notify_subs.user_id``
    (verified-origin), fallback ``delivery_metadata[owui_user_id_attr]``
    (F3, t_cdb88a42) — НЕ ``tasks.created_by``/``assignee`` (профильные
    слэги несравнимы с TG-идентификаторами, аудит §3.3). ``task``
    принимается для сигнатуры будущей интеграции [needs-decision] и
    как контекст вызывающего; само разрешение параметризовано подпиской,
    как в ``_tg_bound_chat_for``.

    Fail-closed (OWUI-only): policy=off / отсутствие owner / отсутствие
    привязки → None. Адрес никогда не фабрикуется и доставляется только
    верифицированному владельцу (H-5, t_efffb731; П.4, t_6692dd10).
    """
    if not isinstance(sub, dict):
        return None
    # F6 (T2 §8.2.2): policy off → OWUI stays OWUI-only; the OWUI→TG mirror
    # lane is never opened (fail-closed).  Mirrors the R1 helper
    # ``_run_branch_lane`` gate so the real production path behaves
    # identically to the unit-tested decision surface.
    if (os.environ.get("HERMES_TELEGRAM_DELIVERY_POLICY") or "").lower() == "off":
        return None
    owner = str(sub.get("user_id") or "").strip()
    if not owner:
        meta = sub.get("delivery_metadata")
        if isinstance(meta, dict):
            from hermes_cli.kanban_db import _OWUI_ATTR_META_KEY
            owner = str(meta.get(_OWUI_ATTR_META_KEY) or "").strip()
    if not owner:
        return None
    tg_lane = (owui_to_tg or {}).get(owner)
    if not tg_lane:
        return None
    # The OWUI→TG mirror lane is resolved purely by the verified owner
    # binding (``owui_to_tg[owner]``): a user who bound their OWUI
    # session/server to a Telegram chat is authorized for that chat
    # (H-5, t_efffb731) — the allowlist must not starve a bound owner of
    # their own итог (П.4, t_6692dd10).  The platform authz layer
    # (TELEGRAM_ALLOWED_USERS) gates the FALLBACK-to-home path, not a
    # user's own verified bound chat, so the two deltas stay consistent.
    return str(tg_lane)


class DeliveryEngine:
    """Single delivery engine: classify -> render -> dispatch (one place)."""

    def __init__(
        self,
        hooks: Optional[DeliveryHooks] = None,
        *,
        allowed_users: Optional[list] = None,
        owui_to_tg: Optional[dict] = None,
        home_channel: str = "",
    ) -> None:
        self.hooks = hooks
        self.allowed_users = allowed_users or []
        self.owui_to_tg = owui_to_tg or {}
        self.home_channel = home_channel or ""
        self._live_interval: Dict[str, int] = {}   # task_id -> current interval
        self._live_since: Dict[str, float] = {}

    # ---- live-status ---------------------------------------------------

    async def ensure_live_status(
        self,
        task: Any,
        sub: dict,
        *,
        send_tg: Any = None,       # callable(adapter, chat_id, text) — first send
        edit_tg: Any = None,       # callable(adapter, chat_id, msg_id, text)
        resolve_tg_adapter: Any = None,   # callable(sub) -> adapter|None
    ) -> None:
        """Own one live-status message for a running task (rule 2, idempotent).

        First tick: insert the row (PK task_id) and send the initial message,
        recording its message_id.  Later ticks: if ``next_update_at`` is due,
        edit the existing message in place; backoff 30s -> x2 -> cap 15m.
        On a closed row do nothing.

        ``send_tg`` / ``edit_tg`` / ``resolve_tg_adapter`` are the same
        injected callbacks the engine's other paths use (``handle_event``
        forwards its own to us).  When omitted we fall back to ``self.hooks``
        so standalone users of the engine (tests, cron) keep working.
        """
        from hermes_cli import kanban_db as _kb

        task_id = getattr(task, "id", None) or sub.get("task_id", "")
        if not task_id:
            return
        platform_str = (sub.get("platform") or "").lower()
        board = sub.get("board") or "default"
        chat_id = sub.get("chat_id") or ""
        thread_id = sub.get("thread_id") or ""
        now = time.time()

        conn = _kb.connect(board=board)
        try:
            row = _kb.get_live_status(conn, task_id)
            if row is not None and int(row["closed"] or 0):
                return
            if row is None:
                _kb.upsert_live_status(
                    conn, task_id, platform_str, chat_id, thread_id,
                )
            # (re)load with our inserted row visible
            row = _kb.get_live_status(conn, task_id)
        finally:
            conn.close()
        if row is None:
            return

        text = render_live_status(task)
        # sanitize every outgoing text (rule 3 mechanical hygiene) even though
        # the template has no free-form fields — title may.
        from hermes_cli.delivery_policy import sanitize_delivery_text

        text = sanitize_delivery_text(text, max_chars=1600)

        msg_id = row["message_id"] if row is not None else None
        next_at = row["next_update_at"] if row is not None else None
        due = next_at is None or now >= float(next_at or 0)

        # TG only: edit-in-place.  OWUI/PWA read the SQL row directly (no
        # chat spam) — the row insert above is the OWUI live status.
        if platform_str == "telegram":
            resolve = resolve_tg_adapter or getattr(
                self.hooks, "resolve_tg_adapter", None,
            )
            if resolve is None:
                return
            try:
                adapter = resolve(sub)
            except TypeError:
                # class-dict lambda bound as an instance method receives
                # two positional args (self+sub).  This is a wiring bug in
                # the caller — degrade to hooks so live-status never silently
                # dies and never double-sends on the next tick.
                adapter = self.hooks.resolve_tg_adapter(sub)
            except Exception:
                return
            if adapter is None:
                return
            if msg_id and not due:
                return
            if msg_id:
                _edit = edit_tg or getattr(self.hooks, "edit_tg", None)
                if _edit is None:
                    return
                res = await _edit(adapter, chat_id, msg_id, text)
                if getattr(res, "success", False):
                    conn = _kb.connect(board=board)
                    try:
                        _kb.bump_live_status_edit(
                            conn, task_id, now + self._next_interval(task_id),
                        )
                    finally:
                        conn.close()
                return
            # first send
            _send = send_tg or getattr(self.hooks, "send_tg", None)
            if _send is None:
                return
            res = await _send(adapter, chat_id, text)
            mid = getattr(res, "message_id", None)
            conn = _kb.connect(board=board)
            try:
                if mid:
                    _kb.set_live_status_message_id(conn, task_id, str(mid))
                _kb.bump_live_status_edit(
                    conn, task_id, now + self._next_interval(task_id),
                )
            finally:
                conn.close()
            return
        # non-TG: just record updated_at so the row is "live"; no chat ping.
        conn = _kb.connect(board=board)
        try:
            _kb.bump_live_status_edit(conn, task_id, now + 60)
        finally:
            conn.close()

    def _next_interval(self, task_id: str) -> int:
        cur = self._live_interval.get(task_id, _LIVE_INTERVAL_S)
        nxt = cur * 2
        if nxt > _LIVE_INTERVAL_CAP_S:
            nxt = _LIVE_INTERVAL_CAP_S
        self._live_interval[task_id] = nxt
        return nxt

    async def close_live_status(self, task_id: str, board: str = "default") -> None:
        """Close the live-status row (on [final]/[aborted])."""
        from hermes_cli import kanban_db as _kb

        conn = _kb.connect(board=board)
        try:
            _kb.close_live_status(conn, task_id)
        finally:
            conn.close()

    # ---- single entry point for the notifier's per-event loop ------------
    #
    # The watcher's internal kind-render + policy-gate + dual-lane body is
    # replaced by ONE ``await engine.handle_event(...)`` (design §г шаг 3.1).
    # The engine decides (classify), renders, and dispatches; the watcher
    # keeps only the cursor mechanics (advance/rewind) driven by the returned
    # action.

    @dataclass
    class HandleResult:
        """Returned per subscription-event; drives the watcher's cursor."""
        action: str          # "advance" | "rewind" | "drop" | "noop"
        decision_type: Optional[str]   # one of EVENT_TYPES or None
        reason: str = ""
        text: str = ""
        tg_suppressed: bool = False
        event_id: Optional[int] = None
        #: True when the TG lane actually accepted the message (adapter
        #: ``SendResult.success is not False``); False when the send was
        #: attempted and FAILED; None when no TG lane was resolved for this
        #: subscription (OWUI-only / policy off) or no send was attempted.
        #: Callers MUST NOT treat None as a failure — the once-only dedup
        #: marker is marked for None (nothing to retry) but withheld for
        #: False so the next watcher tick retries the owner's terminal итог
        #: (t_b154af7d: a ``send_path_degraded`` send used to be dedup-marked
        #: as if delivered and the report was lost forever).
        tg_delivered: Optional[bool] = None
        #: Human-readable adapter error when ``tg_delivered is False``.
        tg_send_error: str = ""
        #: Absolute artifact paths the TG lane actually uploaded (t_e04a8f44).
        #: Filled from ``_deliver_kanban_artifacts`` so the caller/report journal
        #: records what was REALLY delivered, not merely what was discovered.
        artifacts_sent: List[str] = field(default_factory=list)
        #: Artifact paths that were discovered but did NOT reach the chat.
        artifacts_failed: List[str] = field(default_factory=list)
        #: Artifact paths that were discovered but deliberately not uploaded
        #: (missing on disk / above the size cap / unsafe path) — t_e04a8f44.
        artifacts_skipped: List[str] = field(default_factory=list)

    async def handle_event(
        self,
        *,
        sub: dict,
        ev: Any,
        task: Any,
        adapter: Any,
        sub_profile: str,
        board: Optional[str],
        cursor: int,
        old_cursor: int = 0,
        task_is_root: bool = False,
        mission_live: bool = False,
        has_open_descendants: bool = False,
        has_subscribed_descendant: bool = False,
        review_passed: bool = True,
        replace_summary: bool = False,
        task_tg_owner: str = "",
        do_not_deliver: bool = False,
        silent: bool = False,
        platform_str: str = "",
        notifier_profile: str = "",
        send_tg: Any = None,       # callable(adapter, chat_id, text, *a, **k)
        edit_tg: Any = None,       # callable(adapter, chat_id, msg_id, text)
        stage_owui: Any = None,    # callable(text)->(state, delivery_id)
        render_owui: Any = None,   # optional callable(task_id,title,text)->owui text
        wake: Any = None,          # callable(...)
        deliver_artifacts: Any = None,   # async self._deliver_kanban_artifacts
        advance: Any = None,       # sync cursor advance
        rewind: Any = None,        # sync cursor rewind
        tg_allowed: Optional[list] = None,
    ) -> "DeliveryEngine.HandleResult":
        """Classify, render, and dispatch ONE subscription event over its lanes.

        All cursor effects happen through the injected ``advance``/``rewind``
        callables so the watcher's per-sub failure budget and rewind-on-fail
        semantics stay untouched.
        """
        platform_str = platform_str or (sub.get("platform") or "").lower()
        board = board or "default"
        kind = getattr(ev, "kind", None) or ""
        ev_id = getattr(ev, "id", None)

        # Tree context the classifier needs.
        # F9 (t_85a095e0): a card whose OWN subscription is a Terminal event
        # is a branch ROOT — its `completed` is the root's own close and is
        # terminal for the notifier lane when no live goal_mode mission
        # controller keeps the tree in flight.  This mirrors the legacy
        # `root_closing` rule (RF2) and the F9 `_run_f9_owui_lane` decision
        # surface (`branch_terminal = task_is_root and not mission_live`).
        # Descendant cards (silent=True, task_is_root=False) stay
        # intermediate/claimed+silent — the whole-branch итог from the
        # branch-summary watcher is their single channel message.
        #
        # P2 / rule 3 (t_ecab4990, REFACTOR-CHANGELOG R2): for an api_server
        # (OWUI) subscription the PER-EVENT closes of a branch are never the
        # branch итог — the whole-branch итог is delivered exclusively by the
        # branch-summary watcher once the tree fully closes, so the notifier
        # stays silent for api_server roots (no OWUI→TG mirror spam).  A
        # native-TG root subscription delivers its own completion directly
        # (one message per channel, no mirror to suppress).
        #
        # P2 refinement (standalone-OWUI-root, notifier-integration fix): the
        # delegation to the branch-summary watcher only makes sense when the
        # root's subtree actually carries MORE subscribed channels to collapse.
        # A root with NO subscribed descendant (and no open descendant) is the
        # sole subscription of its branch — the branch watcher has nothing to
        # collapse, so the notifier itself stages the root's OWN terminal
        # completion to the OWUI outbox (cursor pinned until the OWUI ACKs).
        # This restores the legacy exact-origin api_server staging behavior
        # (root_closing RF2) without re-opening the per-child mirror spam the
        # P2 rule was added to close: per-child still stays silent because a
        # subscribing child is by definition a subscribed descendant.
        #
        # FIX (t_b7b4995c, native-TG root wake): the previous expression
        # short-circuited to True for EVERY non-api_server platform, so a
        # root's own ``completed`` was classified [final] on the notifier lane
        # even when the branch carried other subscribed channels.  For the
        # primary mission shape that is premature: ``complete_task`` gates a
        # child behind its parents, so a root with children ALWAYS closes FIRST
        # and its children close afterwards; its per-event close is by
        # construction an INTERMEDIATE step of a branch that is still open.
        # On 45dbaf3e3a this spent a creator wake per mission that the real
        # branch итог then repeated — the owner contract is ONE wake per branch
        # итог (t_8788288c / t_0a121f42 «wake только на терминальные итоги
        # ветки»), stated by the replaced pre-R1 predicate as «the SAME routine
        # kinds even for ROOT tasks — under replace mode the root's own
        # completion ping is not the итог: the branch_summary stage owns the
        # single channel message and its wake».
        #
        # The TEXT ping keeps the original classification on purpose: replace
        # mode silences DESCENDANT pings, never the subscribed ROOT's own ping
        # (tests/gateway/test_kanban_branch_summary_watcher.py
        # ::test_replace_mode_suppresses_descendant_notifier_pings pins that the
        # root close reaches its channel).  The wake is gated separately below
        # via ``branch_summary_owns_wake``, which is the actual subject of this
        # card.
        branch_terminal = (
            bool(task_is_root)
            and not bool(mission_live)
            and (
                # Native-TG root: its OWN completion is a real terminal ping
                # for the subscribed channel (one message per channel) — open
                # descendants do not suppress the root's own close; the branch
                # watcher collapses only the DESCENDANT pings.  api_server
                # roots keep P2 semantics: a root-with-open-children close is
                # NOT the branch итог, so the notifier stays silent on both
                # lanes (the branch watcher owns the single terminal итог).
                platform_str != "api_server"
                or not bool(has_open_descendants)
            )
            and (
                platform_str != "api_server"
                or not bool(has_subscribed_descendant)
            )
            # F10 (chamber-graves-ex / sno-economics, 03.11): «завершено» корня,
            # у которого ещё открыты собственные дети, — закрытие ЭТАПА, а не
            # итог миссии.  Правило выше освобождает native-TG от проверки
            # открытых детей, и это верно: у нативной TG-подписки один канал и
            # нет отдельной ветки-итога, поэтому закрытие корня обязано быть
            # видно (иначе владелец не узнает о факте закрытия вообще).
            # Для OWUI (api_server) так нельзя: приёмка/ревью — дочерние
            # карточки той же миссии, и владелец получал «Итоговый отчёт»
            # до её прохождения (ровно инцидент 03.11: S4 «отчёт собран» ушёл
            # в чат, пока S5-приёмка была running).  Итог отдаётся только
            # после закрытия всей ветки — это делает ветка-итог.
            and (
                platform_str != "api_server"
                or not bool(has_open_descendants)
            )
            # ОВНЕР-ПРАВИЛО (03.11.2026): итог «выполнено» проходит только
            # если ревью ветки завершилось ПОЛОЖИТЕЛЬНО. Единственная точка
            # правды — kb.summary_has_passed_review: watcher считает её в
            # _collect и передаёт сюда как ``review_passed`` (t_f6790ef5; до
            # этой карточки параметр не передавался никем и гейт был мёртвым).
            # Приёмка отказом даёт branch_state(outcome="failure") →
            # доставляется как вердикт «❌ Доработка отклонена», а не как итог;
            # здесь глушится только «успех без положительного ревью».
            and bool(review_passed)
        )
        #: The single creator WAKE of a branch belongs to the branch-summary
        #: итог whenever the branch's root did not close last or the branch has
        #: another subscribed channel.  A per-event [final] there is either
        #: premature (branch still open → the итог has not happened yet) or a
        #: duplicate of the итог wake that lands on the next branch tick; both
        #: must stay silent, which is exactly the «no wake on intermediate /
        #: one wake per итог» contract of t_8788288c / t_0a121f42.
        #: A standalone root (sole subscription, no descendants) is NOT covered
        #: — it has no итог to wait for, so its own close keeps its wake.
        branch_summary_owns_wake = bool(task_is_root) and (
            bool(has_open_descendants) or bool(has_subscribed_descendant)
        )
        tree_ctx = {
            "card_status": getattr(task, "status", None) if task else None,
            "block_kind": getattr(task, "block_kind", None) if task else None,
            "is_root": task_is_root,
            "mission_live": mission_live,
            "branch_terminal": branch_terminal,
        }
        dec = classify_event(
            task, ev, tree_ctx,
            silent=bool(silent), do_not_deliver=bool(do_not_deliver),
        )
        if dec.event_type is None:
            if dec.reason not in ("not an engine event", "auto-resolve block"):
                logger.info(
                    "delivery engine: %s/%s silenced (%s)",
                    sub.get("task_id"), kind, dec.reason,
                )
            return self.HandleResult(
                action="advance", decision_type=None, reason=dec.reason,
                event_id=ev_id,
            )

        et = dec.event_type
        title = (task.title if task else sub.get("task_id", "")) or sub.get("task_id", "")
        title = str(title)[:120]

        # ---- live-status: OWUI/PWA read the row; TG edit-in-place ---------
        if et == LIVE_STATUS:
            await self.ensure_live_status(
                task, sub,
                send_tg=send_tg,
                edit_tg=edit_tg,
                resolve_tg_adapter=getattr(
                    self.hooks, "resolve_tg_adapter", None,
                ),
            )
            return self.HandleResult(
                action="advance", decision_type=LIVE_STATUS,
                reason=dec.reason, event_id=ev_id,
            )

        # ---- needs-decision / aborted / final: dedup + dual-lane ---------
        cause_hash = _cause_hash(ev, et)
        r = self.HandleResult(
            action="advance", decision_type=et,
            reason=dec.reason, event_id=ev_id,
        )

        # Close the live row for [final]/[aborted] (rule 2: no more edits).
        if et in (FINAL, ABORTED):
            await self.close_live_status(sub.get("task_id", ""), board)

        # Render the lane text.
        msg = ""
        if et == NEEDS_DECISION:
            msg = render_needs_decision(task, ev)
        elif et == ABORTED:
            msg = render_aborted(task, ev)
        else:  # FINAL
            from hermes_cli.delivery_policy import sanitize_delivery_text
            # F1 (tg-delivery-contract): the итог must carry CONTENT.  The
            # handoff summary is written to the ``completed`` event payload
            # (``payload["summary"]``) by ``kanban_db.complete_task``; an
            # ``Event`` row carries no ``.summary`` attribute, so read it from
            # the payload or the legacy extracted field.  A completed event
            # with a real summary must render it (e.g. a reopened card's
            # second close), never a bare contentless ping.
            _ev_payload = getattr(ev, "payload", None)
            _ev_summary = getattr(ev, "summary", "") or ""
            if not _ev_summary and isinstance(_ev_payload, dict):
                _ev_summary = str(_ev_payload.get("summary") or "").strip()
            _ev_artifacts = (
                _ev_payload.get("artifacts")
                if isinstance(_ev_payload, dict) else None
            )
            msg = render_final(
                _ev_summary or "✅ Задача выполнена",
                summary=_ev_summary,
                artifacts=_ev_artifacts,
            )
            msg = sanitize_delivery_text(msg, max_chars=8000)

        # ---- OWUI lane (api_server): stage exact event into outbox --------
        # Rule-4 decoupling (t_637df501): the OWUI ack gate owns ONLY the OWUI
        # cursor (rewind-to-retry until the server readback ACKs the outbox
        # row).  It must NEVER starve the complementary TG lane or the wake —
        # «[needs-decision] всегда в обе лейны, немедленно» and «карточка в
        # triage часами без уведомления недопустима».  A ``pending`` outbox
        # stage (normal for open blocked/triage cards) previously returned
        # early with ``rewind`` BEFORE the Telegram lane and wake ran, so the
        # author never got the phone ping while OWUI awaited readback.  Now:
        # OWUI pending/error → TG+Wake still fire this tick, and the final
        # result re-claims (rewind) so the OWUI row can be acked on a later
        # drain — bounded by the watcher's MAX_API_STAGE_RETRIES.  The TG
        # dedup marker already written makes the re-claim idempotent (no
        # double ping).
        owui_delivered = False
        owui_rewind: Optional[str] = None   # reason when OWUI needs a re-claim
        if platform_str == "api_server" and stage_owui is not None and ev_id:
            _owui_msg = msg
            if render_owui is not None:
                try:
                    _owui_msg = render_owui(
                        sub.get("task_id", ""), title, msg,
                    )
                except Exception:
                    _owui_msg = msg
            try:
                _state, _ = stage_owui(_owui_msg)
                owui_delivered = _state == "acknowledged"
                if not owui_delivered:
                    # pending: retry-to-ack; unbound/failed: nothing to ack
                    # (advance — no point re-claiming an unwritable row).
                    if _state in ("failed", "unbound", ""):
                        owui_delivered = True
                    else:
                        owui_rewind = f"owui stage {_state or 'pending'}"
            except Exception as exc:
                logger.warning(
                    "delivery engine: OWUI stage failed for %s/%s: %s",
                    sub.get("task_id"), ev_id, exc,
                )
                owui_rewind = "owui stage error"

        # ---- Telegram lane (dual-lane rule 1/4/5) -------------------------
        # native-TG sub: the channel of record IS the TG lane (one message —
        # rule «для native-TG подписок TG-лейна == каналу постановки»).
        # api_server sub: the complementary lane is the owner's bound chat.
        tg_lane = self._tg_bound_chat_for(sub, platform_str)
        tg_adapter = None
        if platform_str == "telegram":
            # The watcher passed the platform adapter already resolved.
            tg_adapter = adapter
        elif tg_lane and self.hooks.resolve_tg_adapter:
            tg_adapter = self.hooks.resolve_tg_adapter(sub)
        if (
            tg_lane
            and tg_adapter is not None
            and send_tg is not None
        ):
            from hermes_cli import kanban_db as _kb
            conn = _kb.connect(board=board)
            try:
                seen = dedup_seen(conn, sub["task_id"], et, cause_hash)
            finally:
                conn.close()
            if not seen:
                try:
                    res = await send_tg(tg_adapter, tg_lane, msg)
                    if getattr(res, "success", True) is False:
                        _ev_err = getattr(res, "error", None) or "unknown"
                        logger.warning(
                            "delivery engine: TG lane send to telegram:%s failed: %s",
                            tg_lane, _ev_err,
                        )
                        if et == FINAL:
                            # t_e04a8f44: a failed terminal send is journaled
                            # (result=failed) — the attempt and its reason stay
                            # auditable even though the cursor rewinds.
                            log_report_delivery(
                                task_id=str(sub.get("task_id") or ""),
                                chat_id=tg_lane,
                                result="failed",
                                reason=str(_ev_err),
                                report_type="none",
                                artifacts=[],
                                text_len=len(msg or ""),
                                trigger="terminal_event",
                                board=board or "",
                            )
                    else:
                        conn = _kb.connect(board=board)
                        try:
                            dedup_mark(conn, sub["task_id"], et, cause_hash)
                        finally:
                            conn.close()
                        _ev_art_res: Optional[dict] = None
                        if et == FINAL and deliver_artifacts is not None:
                            try:
                                _ev_art_res = await deliver_artifacts(
                                    adapter=tg_adapter, chat_id=tg_lane,
                                    metadata={}, event_payload=getattr(ev, "payload", None),
                                    task=task,
                                )
                            except Exception as exc:
                                logger.debug(
                                    "delivery engine: TG lane artifacts for %s failed: %s",
                                    sub["task_id"], exc,
                                )
                                _ev_art_res = {"sent": [], "failed": [], "error": str(exc)}
                        if et == FINAL:
                            # t_e04a8f44: the per-event terminal итог is the
                            # same «финальный отчёт владельцу» delivery as the
                            # branch lane — journal it through the SAME helper
                            # so the audit trail has one shape.
                            _sent_ev = list((_ev_art_res or {}).get("sent") or [])
                            _failed_ev = list((_ev_art_res or {}).get("failed") or [])
                            _skipped_ev = list((_ev_art_res or {}).get("skipped") or [])
                            log_report_delivery(
                                task_id=str(sub.get("task_id") or ""),
                                chat_id=tg_lane,
                                result="delivered",
                                reason=str((_ev_art_res or {}).get("error") or ""),
                                report_type=report_type_of(_sent_ev),
                                artifacts=_sent_ev,
                                artifacts_failed=_failed_ev,
                                artifacts_skipped=_skipped_ev,
                                text_len=len(msg or ""),
                                trigger="terminal_event",
                                board=board,
                            )
                except Exception as exc:
                    logger.warning(
                        "delivery engine: TG lane send failed for %s to telegram:%s: %s",
                        sub["task_id"], tg_lane, exc,
                    )
        # else: no TG lane (no bound owner / no adapter) — best-effort OWUI only.

        # ---- wake (rule 1: [final] terminal + rule 4: [needs-decision]) ----
        if wake is not None:
            want_wake = et in (FINAL, NEEDS_DECISION)
            # FIX (t_b7b4995c): a root's per-event [final] must not spend a
            # SECOND creator turn when the branch-summary итог owns the single
            # wake for that tree.  ``branch_summary_owns_wake`` is True when the
            # branch still had open descendants at this event (the close was
            # premature — the итог has not happened yet) or when another card of
            # the branch is subscribed too (the итог lands on the next branch
            # tick and wakes for the whole tree).  The TEXT ping is unaffected:
            # it keeps its separately accepted contract above.  [needs-decision]
            # is never gated — a human-action event is its own wake cause.
            if et == FINAL and branch_summary_owns_wake:
                want_wake = False
            if want_wake and not do_not_deliver:
                # ``delivery_mode='wake'`` on a push adapter: the wake IS the
                # sole delivery (the text ping was skipped), so a failed wake
                # must rewind the claim — retry next tick instead of losing
                # the event forever (pre-R1 wake-only contract). notify+wake
                # keeps the failed wake best-effort: the text ping already
                # delivered and the cursor may advance.
                _mode = (sub.get("delivery_mode") or "notify").lower()
                _push = getattr(adapter, "supports_async_delivery", True)
                try:
                    _wake_ok = await wake(text=msg)
                except Exception as exc:
                    logger.warning(
                        "delivery engine: wake for %s failed: %s",
                        sub["task_id"], exc,
                    )
                    _wake_ok = False
                if _wake_ok is False and _mode == "wake" and _push:
                    return self.HandleResult(
                        action="rewind", decision_type=et,
                        reason="wake failed (wake-only)", event_id=ev_id,
                    )

        # ---- OWUI cursor decision (t_637df501) ----------------------------
        # TG lane + wake already ran (decoupled from the OWUI ack gate).  Now
        # the OWUI lane's own cursor contract: a ``pending`` stage must re-claim
        # the event so a later drain can ACK the outbox row (bounded by the
        # watcher's MAX_API_STAGE_RETRIES — a permanently-stuck row is dropped
        # there, never pinned forever).  If OWUI was acked (or there was no
        # OWUI lane — native-TG sub, or the sub couldn't stage at all), advance.
        if owui_rewind:
            return self.HandleResult(
                action="rewind", decision_type=et,
                reason=owui_rewind, event_id=ev_id,
            )
        return r

    def _root_completed_empty_summary(self, plan: dict) -> bool:
        """True when the branch root has a recorded ``completed`` event whose
        handoff summary is empty (a genuinely contentless closure: no summary
        text and no first-line preview).  Used by the F1 bare-итог gate to
        distinguish a REAL contentless completion (suppress) from a root with
        no completed record at all (raw SQL close / synthetic readback /
        unknown id — deliver the bare one-liner so the closure is observed).
        """
        from hermes_cli import kanban_db as _kb
        import json as _json

        root_id = plan.get("root_id")
        if not root_id:
            return False
        try:
            conn = _kb.connect(board=plan.get("board"))
            try:
                row = conn.execute(
                    "SELECT payload FROM task_events WHERE task_id=? "
                    "AND kind='completed' ORDER BY id DESC LIMIT 1",
                    (root_id,),
                ).fetchone()
                if row is None or not row[0]:
                    return False
                try:
                    payload = _json.loads(row[0])
                except (TypeError, ValueError):
                    return False
                if not isinstance(payload, dict):
                    return False
                summary = payload.get("summary")
                if isinstance(summary, str) and summary.strip():
                    return False
                preview = payload.get("summary_preview") or payload.get("summary_short")
                if isinstance(preview, str) and preview.strip():
                    return False
                return True
            finally:
                conn.close()
        except Exception:
            return False

    def _tg_bound_chat_for(self, sub: dict, platform_str: str) -> Optional[str]:
        """Resolve the Telegram lane chat for a subscription.

        native-TG sub -> its own chat (single message, rule: «для native-TG
        подписок TG-лейна == каналу постановки»); api_server -> the owner's
        server-bound chat via ``resolve_author_telegram`` (the single owner→TG
        mapping surface; F6 policy-off gate and F3 owner fallback live there).

        Delegates to the module-level helper so the author/owner resolution
        is ONE implementation shared with the [needs-decision] lane
        (карточка 1, критерий 4 — без дублей с OWUI-цепочкой).
        """
        if platform_str == "telegram":
            return str(sub.get("chat_id") or "")
        if platform_str != "api_server":
            return None
        # F6 (T2 §8.2.2): policy off → OWUI stays OWUI-only; the OWUI→TG
        # mirror lane is never opened (fail-closed).  Mirrors the R1 helper
        # ``_run_branch_lane`` gate so the real production path behaves
        # identically to the unit-tested decision surface.
        if (os.environ.get("HERMES_TELEGRAM_DELIVERY_POLICY") or "").lower() == "off":
            return None
        return resolve_author_telegram(task=None, sub=sub, owui_to_tg=self.owui_to_tg)

    async def handle_branch_final(
        self,
        *,
        plan: dict,
        sub: dict,
        msg: str,
        adapter: Any,
        sub_profile: str,
        summary: str = "",
        artifacts: Optional[list] = None,
        wake: Any = None,
        deliver_artifacts: Any = None,
        send_tg: Any = None,
        terminal: bool = False,
        board_override: Optional[str] = None,
        owui_healthy: Optional[bool] = None,
    ) -> "DeliveryEngine.HandleResult":
        """[final] for a closed branch (branch-summary watcher path).

        ``plan`` carries ``root_id``/``board``/``marker_id``; ``msg`` is the
        baked one-liner.  The итог is composed through ``render_final`` with
        the root's summary + artifacts (F1 content contract) and dispatched
        via the same dual-lane table as every other [final].  Returns the
        handle result so the branch watcher can ack the channel or rewind.

        ``owui_healthy`` — состояние OWUI-канала для api_server-подписки
        (``True`` доставлено/подтверждено, ``False`` канал мёртв: ``unbound``
        /``failed``, ``None`` неизвестно).  Управляет только правом гасить
        текстовый успешный итог: если OWUI-канала фактически нет, TG-зеркало
        остаётся единственным путём до владельца и гасить его нельзя.
        """
        board = board_override or plan.get("board") or "default"
        root_id = plan.get("root_id") or sub.get("task_id") or ""
        platform_str = (sub.get("platform") or "").lower()
        res = self.HandleResult(
            action="advance", decision_type=FINAL,
            reason="branch final", event_id=plan.get("marker_id"),
        )
        # Close the live row for the root (branch closed).
        await self.close_live_status(root_id, board)

        # F1 content contract: the итог must carry CONTENT.  The caller may
        # pass pre-built summary/artifacts (it has the DB context); the engine
        # falls back to its own root helpers so branch [final] is rendered
        # here, in the single place that renders [final].
        #
        # Watcher path (no caller-supplied summary/artifacts): resolve the
        # root's own content and decide between the composite and the bare
        # one-liner.  Real artifact files ⇒ the composite итог (header +
        # summary/report) with the files riding ``deliver_artifacts``; no
        # artifacts ⇒ the baked one-liner ``msg`` is the message.  A terminal
        # итог that HAS a completed record but is genuinely contentless
        # (empty summary, no artifacts) is suppressed below with a journal
        # entry; a root with no completed record at all (raw close /
        # synthetic) still delivers the bare one-liner so the closure is
        # observed.
        if summary == "" and artifacts is None:
            try:
                root_paths = owui_root_artifact_paths(plan)
            except Exception as exc:
                logger.warning(
                    "delivery engine: branch final artifact build for %s failed: %s",
                    root_id, exc,
                )
                root_paths = []
            try:
                # F1 (tg-delivery-contract): the terminal итог carries the
                # root's summary/report content.  ``owui_root_final_summary``
                # composes the composite (header + handoff summary / inline
                # report) and degrades to the bare one-liner only when the
                # root is genuinely contentless, so a completed event with a
                # real summary always rides it even without artifact files.
                final_msg = owui_root_final_summary(plan, msg, inline_report=False)
            except Exception as exc:
                logger.warning(
                    "delivery engine: branch final content build for %s failed: %s",
                    root_id, exc,
                )
                final_msg = msg
            artifacts = root_paths
        else:
            final_msg = render_final(
                msg, plan=plan, summary=summary, artifacts=artifacts,
            )

        # OWUI lane (api_server): the caller already stages the итог into the
        # OWUI outbox before calling us (its own stage path) — nothing extra
        # here; the ack is driven by the caller.
        # TG lane
        tg_lane = self._tg_bound_chat_for(sub, platform_str)
        tg_adapter = getattr(self, "_tg_adapter", None)
        if tg_lane and tg_adapter is not None and send_tg is not None:
            if not terminal:
                # intermediate итог: OWUI only, no TG mirror (F6).  Journaled
                # suppression lives ONLY in this module — nothing delivered to
                # TG for a non-terminal branch close.  The single journal
                # record uses the F6/F7-consistent reason (``routine status
                # close``) and the owner-context markers so the audit stream
                # matches the legacy OWUI→TG lane behaviour.
                from hermes_cli.delivery_policy import PolicyDecision
                log_suppression(
                    app="kanban",
                    who=str(sub.get("user_id") or sub.get("chat_id") or ""),
                    target=f"telegram:{tg_lane}",
                    decision=PolicyDecision(
                        action="suppress", reason="routine status close",
                        detail=(
                            "intermediate branch summary — non-terminal "
                            "mission tree suppressed in TG lane"
                        ),
                    ),
                    snippet=(final_msg or "")[:200],
                    extra={
                        "task_id": root_id,
                        "event_kind": "branch_summary",
                        "event_id": plan.get("marker_id") or 0,
                        "owui_to_tg": True,
                    },
                )
                logger.info(
                    "delivery engine: intermediate branch итог %s — OWUI only "
                    "(no TG mirror for non-terminal tree close)",
                    root_id,
                )
                return res
            # F1 bare-итог gate: a TERMINAL итог that recorded a completion
            # but carries NO content (empty handoff summary AND no artifacts)
            # is the contentless ping the owner banned — it is suppressed in
            # the TG lane and journaled, while the channel is still acked so
            # the branch watcher stops retrying.  A root with no completed
            # record at all (raw SQL close, synthetic readback, unknown id)
            # still delivers the bare one-liner — the closure is real and
            # must be observed (F9 leaf-without-controller acceptance).
            bare_itog = (
                (final_msg or "").strip() == (msg or "").strip()
                and (msg or "").strip() == "✅ Задача выполнена"
                and not artifacts
                and self._root_completed_empty_summary(plan)
            )
            if bare_itog:
                from hermes_cli.delivery_policy import PolicyDecision
                log_suppression(
                    app="kanban",
                    who=str(sub.get("user_id") or sub.get("chat_id") or ""),
                    target=f"telegram:{tg_lane}",
                    decision=PolicyDecision(
                        action="suppress", reason="bare terminal итог without content",
                        detail=(
                            "terminal branch summary has no summary and no "
                            "artifacts — suppressed in TG lane (owner contract F1)"
                        ),
                    ),
                    snippet=(final_msg or "")[:200],
                    extra={
                        "task_id": root_id,
                        "event_kind": "branch_summary",
                        "event_id": plan.get("marker_id") or 0,
                        "owui_to_tg": True,
                    },
                )
                logger.info(
                    "delivery engine: bare terminal итог %s suppressed in TG "
                    "lane telegram:%s (no summary, no artifacts)",
                    root_id, tg_lane,
                )
                # Channel is acked regardless — nothing to deliver now nor on
                # a later tick (the bare итог cannot become content later).
                return res
            # ОВНЕР-КОНТРАКТ Telegram (2026-11-04, state.db msg 973591):
            # «в телеграм приходило только по делу — вопросы по миссиям,
            # итоговые артефакты, алерты по невыполнению задач. а служебные и
            # технические сообщения можно гасить».
            #
            # Отсюда ТРИ разрешённых класса и один запрещённый:
            #   1) вопросы по миссиям -> [needs-decision] lane (не здесь);
            #   2) итог с файлом-артефактом -> проходит ниже;
            #   3) алерт по невыполнению -> kind=refusal|failure, проходит
            #      ниже (гейт их НЕ трогает: «❌ Доработка отклонена» — это
            #      именно алерт, а не служебное сообщение);
            #   X) успешный текстовый итог без файла -> служебное сообщение,
            #      гасится: канал молча акается, вердикт остаётся в OWUI.
            #
            # Два ограничения, без которых гейт вредит:
            #  * ``kind == "success"`` — слепое «только с артефактом» убило бы
            #    вердикты ревью, а они почти всегда текстовые: владелец перестал
            #    бы видеть отказы приёмки (это класс 3, а не служебное);
            #  * ``platform_str == "api_server"`` — гасим только ДУБЛЬ в TG-
            #    зеркале OWUI-лейна, у которого есть полноценный OWUI-канал.
            #    Нативная TG-подписка (platform=telegram) — единственный канал
            #    владельца по этой карточке; её молчание не «убирает дубль», а
            #    теряет сигнал целиком (см. F10 выше: закрытие корня обязано
            #    быть видно, иначе владелец не узнает о факте закрытия).
            if (
                not artifacts
                and platform_str == "api_server"
                and str(plan.get("kind") or "success") == "success"
                and owui_healthy is not False
            ):
                from hermes_cli.delivery_policy import PolicyDecision
                log_suppression(
                    app="kanban",
                    who=str(sub.get("user_id") or sub.get("chat_id") or ""),
                    target=f"telegram:{tg_lane}",
                    decision=PolicyDecision(
                        action="suppress",
                        reason="success итог without artifact (owner contract 2026-11-04)",
                        detail=(
                            "успешный terminal-итог без файла-артефакта — "
                            "служебное сообщение, в Telegram не дублируется "
                            "(вердикт остаётся в OWUI). Отказы приёмки и "
                            "падения (refusal/failure) этому гейту не подлежат"
                        ),
                    ),
                    snippet=(final_msg or "")[:200],
                    extra={
                        "task_id": root_id,
                        "event_kind": "branch_summary",
                        "event_id": plan.get("marker_id") or 0,
                        "owui_to_tg": True,
                        "branch_kind": str(plan.get("kind") or "success"),
                    },
                )
                logger.info(
                    "delivery engine: terminal успешный итог %s without "
                    "artifact — OWUI only (no TG mirror, owner contract "
                    "2026-11-04)", root_id,
                )
                res.tg_suppressed = True
                return res
            try:
                resx = await send_tg(tg_adapter, tg_lane, final_msg)
                if getattr(resx, "success", True) is False:
                    err = getattr(resx, "error", None) or "unknown"
                    logger.warning(
                        "delivery engine: branch final TG send to telegram:%s failed: %s",
                        tg_lane, err,
                    )
                    # t_e04a8f44: the send FAILURE is journaled as its own
                    # report-delivery event (result=failed) so an operator sees
                    # the attempt + reason next to the successful ones instead
                    # of only a transient warning.
                    log_report_delivery(
                        task_id=root_id,
                        chat_id=tg_lane,
                        result="failed",
                        reason=str(err),
                        report_type=report_type_of(artifacts),
                        artifacts=[],
                        artifacts_discovered=artifacts or [],
                        text_len=len(final_msg or ""),
                        trigger="branch_terminal",
                        board=board,
                    )
                    # t_b154af7d: report the FAILURE to the caller so the
                    # watcher withholds the once-only dedup marker and retries
                    # on the next tick.  Before this, a degraded send
                    # (``send_path_degraded``) was silently marked as
                    # delivered and the owner's итог was lost for good.
                    res.tg_delivered = False
                    res.tg_send_error = str(err)
                    return res
                else:
                    res.tg_delivered = True
                    _art_res: Optional[dict] = None
                    if deliver_artifacts is not None:
                        try:
                            _art_res = await deliver_artifacts(
                                adapter=tg_adapter, chat_id=tg_lane,
                                metadata={}, event_payload={"artifacts": artifacts or []},
                                task=None,
                            )
                        except Exception as exc:
                            # The итог text landed; a failed attachment must be
                            # observable (a report promised as a file that never
                            # arrives is a silent loss for the owner).
                            logger.warning(
                                "delivery engine: branch final TG artifacts for "
                                "%s/%s failed: %s", root_id, tg_lane, exc,
                            )
                            _art_res = {"sent": [], "failed": list(artifacts or []),
                                        "error": str(exc)}
                    # t_e04a8f44: ONE structured record per terminal delivery —
                    # card/mission, masked chat id, report type, artifacts,
                    # result, time.  Written only after TG accepted the text.
                    _sent = (_art_res or {}).get("sent") if _art_res else []
                    _failed = (_art_res or {}).get("failed") if _art_res else []
                    _skipped = (_art_res or {}).get("skipped") if _art_res else []
                    if _art_res is None:
                        # No artifact lane at all: the text still carries the
                        # итог; record it as a text-only delivery.
                        _sent = []
                    log_report_delivery(
                        task_id=root_id,
                        chat_id=tg_lane,
                        result="delivered",
                        reason=(str((_art_res or {}).get("error") or "")
                                if _failed else ""),
                        report_type=report_type_of(_sent or artifacts),
                        artifacts=_sent or [],
                        artifacts_failed=_failed or [],
                        artifacts_skipped=_skipped or [],
                        text_len=len(final_msg or ""),
                        trigger="branch_terminal",
                        board=board,
                    )
                    res.artifacts_sent = list(_sent or [])
                    res.artifacts_failed = list(_failed or [])
                    res.artifacts_skipped = list(_skipped or [])
            except Exception as exc:
                logger.warning(
                    "delivery engine: branch final TG send failed for %s: %s",
                    root_id, exc,
                )
                log_report_delivery(
                    task_id=root_id,
                    chat_id=tg_lane,
                    result="failed",
                    reason=str(exc),
                    report_type=report_type_of(artifacts),
                    artifacts=[],
                    artifacts_discovered=artifacts or [],
                    text_len=len(final_msg or ""),
                    trigger="branch_terminal",
                    board=board,
                )
                res.tg_delivered = False
                res.tg_send_error = str(exc)
                return res
        if wake is not None and terminal:
            try:
                await wake(text=final_msg)
            except Exception as exc:
                logger.warning(
                    "delivery engine: branch final wake for %s failed: %s",
                    root_id, exc,
                )
        return res

    async def handle_cron_alert(
        self,
        *,
        alert_class: str,       # "aborted" | "needs-decision" | other -> silent
        job: Optional[dict],
        content: str,
        owner_chat: Optional[str],
        allowed_users: Optional[list] = None,
        send_tg: Any = None,
        tg_adapter: Any = None,
        cause_hash: str = "",
        board: str = "default",
    ) -> bool:
        """Cron alert-once via the engine ([aborted]/[needs-decision], rule 4/5).

        Renders the alert text and delivers to the owner's TG chat (the cron
        lane is single — the job's home channel), deduped by the persisted
        (task_id=job-id, kind, cause_hash) marker.  Returns True when the
        alert was delivered (or already seen); False when there is no TG
        lane / adapter.
        """
        if alert_class not in (ABORTED, NEEDS_DECISION):
            return False
        if not owner_chat or tg_adapter is None or send_tg is None:
            return False
        job_id = str((job or {}).get("id") or (job or {}).get("name") or "cron")
        cause_hash = cause_hash or hashlib.sha256(
            (content or "")[:400].encode("utf-8"),
        ).hexdigest()[:24]
        # dedup check
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            if dedup_seen(conn, f"cron:{job_id}", alert_class, cause_hash):
                return True
        finally:
            conn.close()
        text = content[:3000]
        try:
            res = await send_tg(tg_adapter, owner_chat, text)
            if getattr(res, "success", True) is False:
                logger.warning(
                    "delivery engine: cron alert to telegram:%s failed: %s",
                    owner_chat, getattr(res, "error", None) or "unknown",
                )
                return False
            conn = _kb.connect(board=board)
            try:
                dedup_mark(conn, f"cron:{job_id}", alert_class, cause_hash)
            finally:
                conn.close()
            return True
        except Exception as exc:
            logger.warning(
                "delivery engine: cron alert send failed for %s -> telegram:%s: %s",
                job_id, owner_chat, exc,
            )
            return False
