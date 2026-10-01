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
"""


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


def _fallback_children(task_id: str, title: str, body: str, progress: str) -> list[dict]:
    """Deterministic 2-card serial split — no LLM required, always succeeds.

    Child 1 continues the remaining work (primed with the progress note);
    child 2 waits on child 1, then finishes/verifies and reports.
    """
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

    # LLM proposal first; deterministic fallback guarantees the split still
    # happens when the aux LLM is unavailable — the user asked for ALWAYS.
    children = _llm_propose_children(task, progress_note, llm_timeout)
    via = "llm"
    if not children:
        children = _fallback_children(
            task.id, task.title or "", task.body or "", progress_note,
        )
        via = "fallback"

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
                progress_note=progress_note,
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
        reason=f"split into {len(child_ids)} children via {via}",
    )
