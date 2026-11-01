"""Kanban budget split — automatic fan-out when a worker exhausts its
operation budget.

A kanban card "crashes" on budget exhaustion in two places today:

1. goal_mode workers: ``goals.run_kanban_goal_loop`` hits its turn budget
   (``goal_max_turns``) and blocks the card for a human.
2. classic workers: the agent loop hits ``agent.max_turns`` iterations and
   ``agent.turn_finalizer`` records a ``timed_out`` failure (which counts
   toward the circuit breaker and eventually blocks the card).

This module replaces both dead-ends with an automatic split: the card is
fanned out into 2+ child cards (the ORIGINAL card waits for the graph, same
as a triage fan-out root), so the dispatcher immediately re-runs the work
in smaller pieces. No human in the loop.

How the children are chosen:

* An auxiliary LLM (``auxiliary.kanban_budget_splitter.*``, same client as
  the triage decomposer) sees the card title/body, the worker's last
  progress note, and proposes a 2-6 task graph with dependencies.
* If the LLM is unavailable / malformed / returns junk, a deterministic
  fallback splits into exactly 2 serial cards: "continue remaining work"
  then "finish & verify".
* The atomic DB transition is :func:`kanban_db.split_task_on_budget`
  (children in ``todo`` → parent-free ones promoted to ``ready``, run
  closed with ``outcome='budget_split'``, original card waits on the graph).

Runaway-fan-out guard: ``kanban.budget_split_max_depth`` (default 3) — a
task that already sits that many budget splits deep in its lineage refuses
the split and the caller falls back to the legacy blocked/failure path.
``kanban.budget_split: false`` disables the whole mechanism (legacy
behaviour).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are the Kanban budget splitter for the Hermes Agent board.

A worker just exhausted its operation budget (turn/iteration limit) on a
kanban card WITHOUT completing it. The card must be split into 2 or more
smaller child cards, each small enough to finish comfortably within one
worker budget. No human is available — decide yourself.

You will be given:
  - The original card title and body
  - The worker's last progress note / partial response (may be empty)

Output a single JSON object with this exact shape:

  {
    "rationale": "<one sentence>",
    "tasks": [
      {
        "title": "<concrete task title, imperative voice, <= 80 chars>",
        "body":  "<detailed spec for a FRESH worker with no other context: goal, approach, acceptance criteria. Reference concrete file paths/artifacts from the progress note when relevant.>",
        "parents": [<int>, ...]
      },
      ...
    ]
  }

Rules:
  - Exactly 2 to 4 tasks. Each must be strictly smaller than the original.
  - EXCEPTION — the close-only case: when the progress note states that the
    work is actually FINISHED and only the card is still open (artifact
    produced, tests run, nothing left to implement), do NOT decompose it.
    Output exactly ONE task titled "verify and close: <title>" whose body says
    the artifact already exists and must NOT be redone. Re-splitting finished
    work into 2-4 fresh workers is the failure this rule exists to stop.
  - "parents" is a list of 0-based INDICES into this same "tasks" list.
    Tasks with no parents run in PARALLEL. Tasks with parents wait until
    every parent completes.
  - Preserve unfinished work from the progress note: the first child should
    pick up where the worker stopped, not restart from scratch.
  - The children together must cover ALL remaining work of the original card.
  - Keep the assignee implied by the card: each child runs under the same
    profile, so do not invent cross-profile handoffs.

No preamble, no closing remarks, no code fences. Output only the JSON object.
"""


_USER_TEMPLATE = """Task id: {task_id}
Title: {title}
Body:
{body}

Worker's last progress note (partial, budget ran out mid-work):
{progress}

Rule that overrides everything above: if the progress note says the work is
actually FINISHED and only the card is still open (artifact built, tests run,
only a verify-and-close step left), do NOT split it into parts — output
EXACTLY ONE task titled "verify and close: <title>" that re-uses the finished
artifact and does not redo it. Otherwise output 2-4 tasks as specified.
"""

# A note resolves to the single "verify and close" card only when it names a
# CLOSING REMAINDER — the only thing still open is the card itself.
#
# Bare completion phrasing («работа выполнена», «всё сделано», «already
# completed») is deliberately NOT sufficient. It is the normal way a worker
# OPENS a partial-credit summary («работа выполнена частично, осталось
# доделать раздел 3»), so treating it as close-only shipped children whose body
# said both «осталось доделать раздел 3» and «ALREADY DONE — DO NOT redo», and
# the remainder got no card at all (review of 15743759b0: 11 of 22 realistic
# partial-credit tails collapsed; the pre-fix revision split them correctly).
_CLOSE_ONLY_NO_WORK_LEFT = (
    "nothing left to implement",
    "nothing left to do",
    "nothing left to change",
    "only the card is still open",
    "only the card remains",
    "переделывать не нужно",
    "не нужно переделывать",
)

# «осталось (только|лишь)? закрыть» ENDING its clause, optionally followed by
# the card artifact as the LAST word of that clause.
#
# What counts as the end of the clause: sentence punctuation (`.!?…`), a
# closing bracket, a quote, a line break, or end of text. `:` and `;` are
# deliberately EXCLUDED — they introduce an enumeration of what still remains
# («осталось закрыть: раздел 3, тесты»), which is the single most common way a
# worker lists unfinished work. Treating them as a clause end collapsed 6 of 14
# realistic mid-work tails in review of c2338e94d1, so the named remainder got
# no card at all while its child body claimed «ALREADY DONE — DO NOT redo».
#
# Deliberately NOT accepted: closing a WORK ITEM — «осталось закрыть раздел 3»,
# «осталось закрыть миграции», «осталось закрыть тестами функцию parse()»,
# «осталось закрыть вопрос с лицензией», «осталось закрыть задачу миграции на
# прод». The artifact word («карточку / тикет / задачу») only counts when
# nothing is attached to it inside the same clause: «осталось закрыть карточку
# и раздел 3» keeps working because the clause continues past «карточку».
# The asymmetry is intentional: a false positive drops unfinished work with no
# card at all, while a false negative only costs one redundant split.
_CLOSE_REMAINDER_RE = re.compile(
    r"осталось\s+(?:только\s+|лишь\s+)?закрыть"
    r"(?:\s+(?:карточк[уа]|тикет[а]?|задач[у]))?"
    r"\s*(?:[.!?…)}\»\"']|\n|$)",
    re.IGNORECASE,
)

# A later clause that RE-OPENS the remainder («Осталось закрыть.
# Осталось: раздел 3 и тесты», «осталось закрыть карточку. Но осталось:
# раздел 3»), and a partial-credit remainder stated BEFORE the closer
# («Осталось: раздел 3. Осталось закрыть карточку», «Раздел 3 не начат.
# Осталось закрыть карточку») both mean the card is not the only thing left.
_CLOSE_REMAINDER_REOPENS_RE = re.compile(
    # «Осталось:» / «Осталось закрыть: раздел 3» / «Остались: тесты» — a
    # remainder introduced by a colon within a short window after «остал(о|и)сь».
    r"остал[оа]сь\b[^.\n]{0,40}?[:;]",
    re.IGNORECASE,
)
# Work-naming verb immediately before the closer («надо дописать, осталось
# закрыть») is not required to veto — the negations/partial markers cover the
# explicit forms; the two patterns above cover the structural ones.

# Negation and partial-credit qualifiers disqualify the note OUTRIGHT, checked
# before any positive signal: a note that says the work is not finished must
# never resolve to the close-only card, even when a positive marker appears in
# the same text.
_CLOSE_ONLY_NEGATIONS = (
    "не полностью",
    "не выполнена",
    "не выполнено",
    "не готов",
    "не готова",
    "не готово",
    "не закончена",
    "не закончено",
    "не завершена",
    "не завершено",
    "не всё сделано",
    "не все сделано",
    "не всё готово",
    "не все готово",
    "ещё не готово",
    "еще не готово",
    "not complete",
    "not fully",
    "not finished",
    "not done",
    "incomplete",
    "still remaining",
)

# Partial-credit qualifiers. Each of these says "some of it is done" — which is
# the opposite of close-only — and they are the qualifier class the previous
# revision missed («частично / на 70% / в основном / кроме / наполовину»).
_CLOSE_ONLY_PARTIAL_MARKERS = (
    "частично",
    "наполовину",
    "в основном",
    "не до конца",
    "кроме",
    "за исключением",
    "в работе",
    "не начат",
    "не начато",
    "не написано",
    "не написаны",
    "не реализовано",
    "не реализован",
    "не подключ",
    "не проверял",
    "черновик",
    "первую половину",
    "осталось доделать",
    "осталось дописать",
    "осталось написать",
    "осталось реализовать",
    "осталось починить",
    "надо дописать",
    "надо починить",
    "требует решения",
    "not started",
    "in progress",
    "partly",
    "partially",
    "still open for part",
)
# «на 70%», «70 %» — a numeric share below 100 means partial. 100% is not
# matched on purpose (it is not a partial-credit signal).
_CLOSE_ONLY_PARTIAL_PERCENT_RE = re.compile(r"(?:^|[^\d])[1-9]?\d\s*%")


def _progress_reports_completion(progress: str) -> bool:
    """True only when the note says the work is done AND just closing remains.

    Two conditions must BOTH hold, and any negation/partial-credit qualifier
    vetoes first:

    1. the note names a *closing remainder* — «осталось (только) закрыть»
       ENDING its clause (optionally «карточку/тикет/задачу» as the last word)
       — or an explicit "nothing left to implement" statement;
    2. no negation («не полностью») and no partial-credit qualifier
       («частично», «на 70%», «кроме», «наполовину», «не начат») appears.

    Structural vetoes on top of the qualifier lists, because the remainder is
    often enumerated rather than qualified: a closer that does NOT end its
    clause («осталось закрыть: раздел 3, тесты» — `:`/`;` introduce the list)
    and a note that re-opens the remainder in another clause («Осталось
    закрыть.\\nОсталось: раздел 3») are both mid-work, not close-only.

    Deliberately narrow — it keys on the closing remainder, NOT on completion
    phrasing and NOT on the ``ВЫПОЛНЕНО РАНЕЕ`` header that
    :func:`agent.turn_finalizer._collect_budget_progress_note` always emits
    (that header means "this part already landed, continue from here", so
    matching it would collapse every real split into a single card).

    The asymmetry is intentional: a false positive drops the remaining work
    entirely (no card at all), a false negative costs one redundant split.
    """
    if not progress or not isinstance(progress, str):
        return False
    low = progress.lower()
    if any(neg in low for neg in _CLOSE_ONLY_NEGATIONS):
        return False
    if any(bad in low for bad in _CLOSE_ONLY_PARTIAL_MARKERS):
        return False
    if _CLOSE_ONLY_PARTIAL_PERCENT_RE.search(low):
        return False
    if any(phrase in low for phrase in _CLOSE_ONLY_NO_WORK_LEFT):
        return True
    if _CLOSE_REMAINDER_REOPENS_RE.search(low):
        return False
    return _CLOSE_REMAINDER_RE.search(low) is not None


@dataclass
class BudgetSplitResult:
    """Outcome of a budget-split attempt."""

    ok: bool
    child_ids: Optional[list[str]] = None
    reason: str = ""
    via: str = ""  # "llm" | "fallback" | "" (on failure)


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _extract_json_blob(raw: str) -> Optional[dict]:
    if not raw:
        return None
    import json
    import re

    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    candidate = stripped[first : last + 1]
    try:
        val = json.loads(candidate)
    except (ValueError, Exception):
        return None
    return val if isinstance(val, dict) else None


def _load_config() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def budget_split_enabled(cfg: Optional[dict] = None) -> bool:
    """``kanban.budget_split`` — default True (the mechanism is opt-OUT)."""
    if not isinstance(cfg, dict):
        cfg = _load_config()
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    if isinstance(kanban_cfg, dict) and "budget_split" in kanban_cfg:
        return bool(kanban_cfg.get("budget_split"))
    return True


def _budget_split_max_depth(cfg: Optional[dict] = None) -> int:
    if not isinstance(cfg, dict):
        cfg = _load_config()
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    try:
        val = int(kanban_cfg.get("budget_split_max_depth", kb.DEFAULT_BUDGET_SPLIT_MAX_DEPTH))
    except (TypeError, ValueError):
        val = kb.DEFAULT_BUDGET_SPLIT_MAX_DEPTH
    return max(1, val)


def _close_only_child(task_id: str, title: str, body: str, progress: str) -> dict:
    """The single "verify and close" card for an already-finished split."""
    safe_title = _truncate(title or "task", 100)
    return {
        "title": f"[close only] Verify and close: {safe_title}",
        "body": (
            "Parent card: " + task_id + "\n"
            "Original title: " + safe_title + "\n\n"
            "Original body:\n" + (body or "(no body)")
            + "\n\nProgress note from the previous worker (budget ran out "
            "mid-work — continue from here, do NOT restart from scratch "
            "unless the state is broken):\n"
            + _truncate(progress, 2000)
            + "\n\nYOUR JOB: the work described above is ALREADY DONE — the "
            "artifact exists and was produced by the previous worker. DO NOT "
            "redo it, do not rewrite the artifact, do not start from scratch. "
            "Only verify the acceptance criteria from the original body "
            "against what already exists, then close the card with a short "
            "verification summary (what was checked, with what result). If "
            "you find a genuine defect, fix exactly that defect and nothing "
            "else."
        ),
        "parents": [],
    }


def _fallback_children(task_id: str, title: str, body: str, progress: str) -> list[dict]:
    """Deterministic split — no LLM required, always succeeds.

    Child 1 continues the remaining work (primed with the progress note);
    child 2 waits on child 1, then finishes/verifies and reports.

    Exception (class C): when the progress note reports the work as actually
    finished with only the card still open, resolve to ONE "verify and close"
    child instead of 2-4 — re-splitting finished work is the failure mode that
    produced 16 cards on t_2d037564.
    """
    if _progress_reports_completion(progress):
        return [_close_only_child(task_id, title, body, progress)]
    safe_title = _truncate(title or "task", 100)
    progress_block = ""
    if progress:
        progress_block = (
            "\n\nProgress note from the previous worker (budget ran out "
            "mid-work — continue from here, do NOT restart from scratch "
            "unless the state is broken):\n"
            + _truncate(progress, 2000)
        )
    base_body = (
        "Parent card: " + task_id + "\n"
        "Original title: " + safe_title + "\n\n"
        "Original body:\n" + (body or "(no body)")
        + progress_block
    )
    return [
        {
            "title": f"[split 1/2] Continue remaining work: {safe_title}",
            "body": (
                base_body
                + "\n\nYOUR JOB: inspect the current state of the work "
                "(files, artifacts, board comments of the parent card), "
                "complete the first half of the remaining work, and leave "
                "a clear handoff comment for part 2 (what is done, what "
                "remains). Verify what you produce; do not leave partial "
                "or unverified output."
            ),
            "parents": [],
        },
        {
            "title": f"[split 2/2] Finish and verify: {safe_title}",
            "body": (
                base_body
                + "\n\nYOUR JOB: part 1 (the parent split card "
                "'[split 1/2]') has already run. Read its comments and the "
                "current state, complete ALL remaining work of the "
                "original card, run the acceptance criteria from the "
                "original body, and finish with a short verification "
                "summary (what was checked, with what result)."
            ),
            "parents": [0],
        },
    ]


def _llm_propose_children(task, progress: str, timeout: Optional[int]) -> Optional[list[dict]]:
    """Ask the auxiliary splitter LLM for a 2-4 task graph. None on any failure."""
    try:
        from agent.auxiliary_client import call_llm  # type: ignore
    except Exception as exc:
        logger.debug("budget_split: auxiliary client import failed: %s", exc)
        return None

    user_msg = _USER_TEMPLATE.format(
        task_id=task.id,
        title=_truncate(task.title or "", 400),
        body=_truncate(task.body or "(no body)", 4000),
        progress=_truncate(progress or "(no progress note captured)", 3000),
    )
    try:
        resp = call_llm(
            task="kanban_budget_splitter",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=4000,
            timeout=timeout or 120,
        )
    except Exception as exc:
        logger.info("budget_split: API call failed for %s (%s)", task.id, exc)
        return None

    try:
        raw = resp.choices[0].message.content or ""
    except Exception:
        return None
    parsed = _extract_json_blob(raw)
    if parsed is None:
        return None

    raw_tasks = parsed.get("tasks")
    if not isinstance(raw_tasks, list) or not (2 <= len(raw_tasks) <= 4):
        return None
    children: list[dict] = []
    for idx, entry in enumerate(raw_tasks):
        if not isinstance(entry, dict):
            return None
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            return None
        body = entry.get("body")
        parents = entry.get("parents") or []
        if not isinstance(parents, list):
            parents = []
        clean_parents = [
            p for p in parents
            if isinstance(p, int) and 0 <= p < len(raw_tasks) and p != idx
        ]
        children.append({
            "title": title.strip()[:200],
            "body": body if isinstance(body, str) else "",
            "parents": clean_parents,
        })
    return children


def _profile_author() -> str:
    import os

    return (
        os.environ.get("HERMES_PROFILE")
        or os.environ.get("USER")
        or "budget-splitter"
    )


# Sources of a recovered progress note, in priority order. Recorded on the
# ``budget_split`` event as ``progress_note_source`` so the "note went missing"
# class of failure is measurable instead of invisible.
PROGRESS_SOURCE_RUN_BUFFER = "run_buffer"
PROGRESS_SOURCE_PARENT_COMMENT = "parent_comment"
PROGRESS_SOURCE_WORKSPACE = "workspace"
PROGRESS_SOURCE_NONE = "none"

_RECOVERY_COMMENT_LIMIT = 1500
_RECOVERY_WORKSPACE_FILES = 20


def _recover_from_parent_comment(conn, task_id: str) -> str:
    """Last non-trivial comment on the card (the worker's own handoff notes)."""
    try:
        comments = kb.list_comments(conn, task_id) or []
    except Exception:
        return ""
    for comment in reversed(comments):
        body = getattr(comment, "body", "") or ""
        if len(body.strip()) >= 40:
            return body.strip()[-_RECOVERY_COMMENT_LIMIT:]
    return ""


def _recover_from_workspace(workspace_path: Optional[str]) -> str:
    """Recent change inventory of the card's workspace (``git status`` tail)."""
    if not workspace_path:
        return ""
    import os
    import subprocess

    if not os.path.isdir(workspace_path):
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace_path), "status", "--short"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    lines = [ln.rstrip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    shown = lines[:_RECOVERY_WORKSPACE_FILES]
    return (
        "Рабочий каталог карточки содержит изменённые/новые файлы "
        f"({workspace_path}, git status --short, {len(lines)} записей):\n"
        + "\n".join(f"- {ln}" for ln in shown)
    )


def _resolve_progress_note(task, progress_note: str) -> tuple[str, str]:
    """Return ``(note, source)`` — the caller's note or a recovered one.

    Only runs when the caller passed an empty note (the turn_finalizer seam
    always passes one when the run left any trace). Recovery order: the card's
    last substantive comment, then the card workspace's change inventory. An
    exhausted card keeps splitting either way — the note is a handoff, not a
    gate — but the recorded source makes ``"none"`` countable.
    """
    note = (progress_note or "").strip()
    if note:
        return note, PROGRESS_SOURCE_RUN_BUFFER
    try:
        with kb.connect_closing() as conn:
            note = _recover_from_parent_comment(conn, task.id)
    except Exception:
        note = ""
    if note:
        return note, PROGRESS_SOURCE_PARENT_COMMENT
    note = _recover_from_workspace(getattr(task, "workspace_path", None))
    if note:
        return note, PROGRESS_SOURCE_WORKSPACE
    return "", PROGRESS_SOURCE_NONE


def run_budget_split(
    task_id: str,
    *,
    source: str,
    progress_note: str = "",
    expected_run_id: Optional[int] = None,
    cfg: Optional[dict] = None,
    llm_timeout: Optional[int] = 120,
) -> BudgetSplitResult:
    """Try to split ``task_id`` after operation-budget exhaustion.

    ``progress_note`` is the deterministic handoff captured by the caller
    (``agent.turn_finalizer``) from the exhausted run. When it is empty this
    function recovers one from the card's last comment or workspace before
    splitting, and records where the note came from.

    Returns ``BudgetSplitResult``. On ``ok=False`` the caller MUST fall
    back to its legacy terminal path (block / failure record) — a failed
    split must never strand a card.
    """
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    if task is None:
        return BudgetSplitResult(False, reason="unknown task id")

    if cfg is None:
        cfg = _load_config()
    if not budget_split_enabled(cfg):
        return BudgetSplitResult(False, reason="budget_split disabled in config")

    note, note_source = _resolve_progress_note(task, progress_note)
    close_only = _progress_reports_completion(note)

    # LLM proposal first; deterministic fallback guarantees the split still
    # happens when the aux LLM is unavailable — the user asked for ALWAYS.
    # The close-only class skips the LLM entirely: there is nothing to
    # decompose, and asking would risk a 2-4 card fan-out over finished work.
    children = None
    via = "fallback"
    if not close_only:
        children = _llm_propose_children(task, note, llm_timeout)
        via = "llm"
    if not children:
        children = _fallback_children(
            task.id, task.title or "", task.body or "", note,
        )
        via = "fallback"
        if close_only:
            via = "close_only"
    # The LLM may return more than one card for an already-finished task; the
    # close-only contract wins regardless of which path produced the proposal.
    if _progress_reports_completion(note) and len(children) != 1:
        children = [_close_only_child(
            task.id, task.title or "", task.body or "", note,
        )]
        via = "close_only"

    try:
        with kb.connect_closing() as conn:
            child_ids = kb.split_task_on_budget(
                conn,
                task_id,
                children=children,
                author=_profile_author(),
                expected_run_id=expected_run_id,
                source=source,
                max_depth=_budget_split_max_depth(cfg),
                progress_note=note,
                progress_note_source=note_source,
                allow_single=len(children) == 1,
            )
    except Exception as exc:
        logger.exception("budget_split: DB error on task %s", task_id)
        return BudgetSplitResult(False, reason=f"DB error: {type(exc).__name__}")

    if not child_ids:
        return BudgetSplitResult(
            False,
            reason="split refused (not running / run mismatch / max depth)",
        )
    return BudgetSplitResult(
        True, child_ids=child_ids, via=via,
        reason=(
            f"split into {len(child_ids)} children via {via} "
            f"(progress source: {note_source}, {len(note)} chars)"
        ),
    )
