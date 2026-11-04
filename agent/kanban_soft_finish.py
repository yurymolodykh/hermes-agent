"""Soft finish threshold for kanban workers («финишируй на 80% бюджета»).

Why
---
Cards systematically died *after* the work was already done: the worker
consumed its whole iteration / wall-clock budget, then ``turn_finalizer``
recorded ``timed_out`` ("Iteration budget exhausted (200/200)") even though
the deliverable existed on disk and the model knew what to report.

Root cause (t_ed744fd2): every existing stop-time nudge
(``kanban_stop``, ``verification_stop``, ``pre_verify``) is a **turn-end**
gate — it only runs when the model tries to finish cleanly. A worker that
hits the hard budget never reaches a turn end, so none of them can fire.
The only mid-run pressure signal in the core is
``conversation_loop._maybe_inject_run_budget_wrapup``, and it is dormant for
kanban workers because ``agent.run_budget_seconds`` is unset (workers get no
``--run-budget`` and no ``agent.run_budget_seconds`` in config).

What this module does
---------------------
A **soft finish** directive: at ~80% of the run's real budget (steps
``api_call_count / agent.max_iterations`` and, when the card carries a
``max_runtime_seconds``, wall clock) the worker is told, in-band, to call
``kanban_complete`` NOW with the result it already has:

* the threshold is computed from the run's actual budget, never a constant;
* the directive is injected **once** (latch ``_kanban_soft_finish_injected``)
  into the newest ``role:"tool"`` message — the same cache-safe channel
  ``/steer`` and the run-budget wrap-up notice use, so role alternation and
  the prompt-cache prefix survive;
* firing writes an explicit ``INFO`` log line (``kanban soft finish: ...``)
  for later audit;
* if the worker still has not called a terminal board tool within
  ``kanban.soft_finish_grace_steps`` steps after the directive (or the hard
  budget runs out first), the card is closed **done** with the summary the
  worker already produced instead of being recorded ``timed_out``.

Policy-only, like :mod:`agent.kanban_stop`: it never changes the board unless
the caller asks for :func:`force_complete`, and that path re-checks the
authenticated worker binding before touching anything, so a stale inherited
env can never close a foreign card.

Configuration (``config.yaml`` → ``kanban``)
--------------------------------------------
``soft_finish``               master switch (default ``true``)
``soft_finish_ratio``         fraction of the budget that triggers it (0.8)
``soft_finish_grace_steps``   steps allowed after the directive (12)

Worker env (set by ``kanban_db._default_spawn``)
------------------------------------------------
``HERMES_KANBAN_MAX_RUNTIME_SECONDS``  card's ``max_runtime_seconds`` (absent = none)
``HERMES_KANBAN_RUN_STARTED_AT``       epoch seconds when the attempt started
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Iterable, NamedTuple, Optional

from agent import kanban_stop as _ks

logger = logging.getLogger(__name__)

DEFAULT_SOFT_FINISH_RATIO = 0.80
DEFAULT_SOFT_FINISH_GRACE_STEPS = 12
# A card whose step budget is at or below this floor never gets the step arm.
# A 20-step routine card is 16 steps from death at 80% — directing it to stop
# and complete is pure noise (acceptance #3: a short routine card must not see
# the directive and must never be force-closed). The threshold should mean
# something in absolute terms, not only in percentages.
DEFAULT_SOFT_FINISH_MIN_BUDGET_STEPS = 60
_MIN_RATIO = 0.10
_MAX_RATIO = 0.95

# Directive text. Deliberately imperative and narrow: the failure mode being
# fixed is a worker that keeps polishing / re-checking past the point where the
# deliverable is complete.
SOFT_FINISH_DIRECTIVE = (
    "[SYSTEM DIRECTIVE — kanban budget {pct}% used ({used}/{budget}{time_part})] "
    "Stop all new work now. Do NOT start new checks, do NOT re-read sources, "
    "do NOT rewrite text that is already written.\n"
    "If the deliverable is NOT on disk yet (a verdict, report, or review you "
    "have not written down), the deliverable IS what you produce next: write "
    "it now, item by item, from the evidence you have already collected. Name "
    "every item you could not check in an explicit 'НЕ ПРОВЕРЕНО' / 'NOT "
    "CHECKED' list at the end. An honest partial verdict with an explicit "
    "unchecked list is accepted; silence after a full budget is not.\n"
    "Record each item the moment you decide it — `kanban_comment(body=\"verdict: "
    "<item> — <result>\")`, or a comment that opens as «Вердикт …» / «VERDICT …», "
    "one comment per decided item — so an aborted run still leaves every "
    "decision on the board.\n"
    "Your very next action must be `kanban_complete(summary=..., "
    "artifacts=[...])` using the result you already have — even if the "
    "remaining polish is unfinished. A card that finishes with an honest "
    "partial result is accepted; a card that runs out of budget is recorded "
    "as `timed_out` and its work is at risk.\n"
    "If the work genuinely cannot be completed, call `kanban_block(reason=...)` "
    "instead — but do not spend another step exploring."
)

# Durable partial-verdict channel. A review worker that burns its budget while
# re-verifying leaves nothing behind unless each decided item was written down
# as it was decided: the transcript dies with the run and the scratch workspace
# is rmtree'd after the close (t_24120021). Board comments ARE durable, so a
# one-line comment per decided item is the cheapest record that survives.
VERDICT_NOTE_PREFIX = "verdict:"
# The prefix is a convention, not a contract: a worker that writes a real
# partial verdict in its own words must still be harvested. The actual reviewer
# of t_39fcfc77 committed «**Промежуточный вердикт … часть 1/3**» and
# «ВЕРДИКТ НЕЗАВИСИМОЙ ПРИЁМКИ … часть 2/2» — neither starts with the literal
# prefix, so a prefix-only harvester returned 0 items for precisely the run this
# channel exists to protect.
#
# Matching is HEAD-ANCHORED on purpose. A substring search over the whole body
# would classify «обычный комментарий, не вердикт» as a verdict note (caught by
# tests/agent/test_kanban_soft_finish.py); the marker has to open the comment,
# after stripping markdown decoration.
VERDICT_NOTE_MARKERS = (
    "verdict",
    "вердикт",
    "промежуточный вердикт",
    "частичный вердикт",
    "предварительный вердикт",
)
_VERDICT_DECORATION = "*#►•-–— \t«\"'`(>_"
# How far into the comment the opening marker may sit. Keeps a long preamble
# from qualifying while still allowing a short breadcrumb before the heading.
_VERDICT_MARKER_WINDOW = 200
# A comment that OPENS as «Вердикт не вынесен» is the exact inverse of a
# recorded verdict: the worker reports that there is NO verdict and the close
# would then report that there is one (t_6c41691e D1, run 6008's incident
# shape). Negation is therefore rejected after the decoration is stripped.
#
# The hard part is that the negated forms come in two different SHAPES, and a
# single "does the window contain a negator" test gets one of them wrong:
#
#   A. the marker's own clause carries it — «Вердикт не вынесен»,
#      «Вердикта нет.», «Вердикт по существу не вынесен»;
#   B. the marker is followed by a colon and the CONTENT itself is the
#      negation — «Вердикт: не вынесен», «Вердикт: пока нет».
#
# Shape B is what a naive clause-scoped test misses (the clause ends at the
# colon, so «вердикт» alone looks positive). But a colon alone cannot decide
# it either: a genuine verdict legitimately writes «Вердикт: пункт 3 — не готов
# к выпуску» and «Вердикт: пока нет данных по пункту 4, остальное принято».
# The rule that separates them: after a colon the negation must BE the whole
# first content segment. «не вынесен» alone → no verdict; «не вынесен» followed
# by an object/continuation → a decided item that happens to be negative.
_NEGATION_VERB = (
    r"(?:"
    r"не\s+(?:вынесен\w*|выношу|выносится|выносились|готов\w*|сформирован\w*"
    r"|будет|состоится|состоялся|берусь|представляется)"
    r"|пока\s+нет"
    r"|not\s+(?:yet\s+)?(?:issued|ready)|no\s+verdict|pending"
    r")"
)
# Shape A, at the marker itself: «вердикта нет», «вердикт не будет».
_VERDICT_NEGATION_MARKER_CLAUSE = re.compile(
    r"(?:^|\s)(?:" + _NEGATION_VERB + r"|нет)\b", re.IGNORECASE
)
# Shape B: the entire first content segment after the colon is the negation.
_VERDICT_NEGATION_CONTENT_SEGMENT = re.compile(
    r"^" + _NEGATION_VERB + r"$", re.IGNORECASE
)
# Unambiguous phrasings that do not depend on where the marker sits.
_VERDICT_NEGATION_EXPLICIT = re.compile(
    r"(вердикт\w*\s+нет\b"
    r"|вердиктовать\s+не"
    r"|не\s+берусь"
    r"|no\s+verdict"
    r"|verdict\s+(?:not\s+(?:yet\s+)?issued|was\s+not\s+issued))",
    re.IGNORECASE,
)
# Where the marker's own clause ends. A colon starts the verdict's CONTENT
# («Вердикт: …»), so everything after it is result text, not a qualification
# of whether the verdict exists.
_VERDICT_CLAUSE_BREAKS = (":", "\n", "\r", ".", "—", "–", ";")
# Where the first CONTENT segment ends, used by the shape-B test.
_VERDICT_CONTENT_BREAKS = (",", "\n", "\r", ".", ";", "—", "–", ")", "(")
# How much of a harvested note the forced-close summary shows per item. The
# full text stays in the board comment; the summary only has to make the close
# self-explanatory to a human reading the card.
_VERDICT_SUMMARY_NOTE_MAX_CHARS = 600
_VERDICT_NOTE_HEADER = (
    "Вердикт, зафиксированный по частям в комментариях доски "
    "(переживает уборку рабочего каталога):"
)


def _verdict_marker_at(head: str) -> Optional[tuple[int, str]]:
    """``(index, marker)`` of the opening verdict marker in ``head``, or None.

    Decorations are stripped from the front only, so the marker must be the
    first word of the comment (after ``**``, ``#``, ``«`` …).
    """
    for marker in VERDICT_NOTE_MARKERS:
        if head.startswith(marker):
            return 0, marker
    return None


def _first_segment(text: str, breaks: tuple[str, ...]) -> str:
    """Text up to the earliest occurrence of any separator in ``breaks``."""
    cut = len(text)
    for sep in breaks:
        at = text.find(sep)
        if at != -1:
            cut = min(cut, at)
    return text[:cut]


def _verdict_is_negated(text: str) -> bool:
    """True when the comment's HEAD says there is no verdict.

    Only the opening window is inspected: a real verdict may discuss a missing
    verdict deep in the body («пункт 4: вердикт не вынесен, нужны данные»)
    without becoming a non-verdict.

    Inside that window both negated shapes are rejected (see the block comment
    above on ``_NEGATION_VERB``), while a verdict whose CONTENT is a negative
    finding («Вердикт: пункт 3 — не готов к выпуску») is still harvested.
    """
    head = text[:_VERDICT_MARKER_WINDOW].lstrip(_VERDICT_DECORATION).lower()
    if _VERDICT_NEGATION_EXPLICIT.search(head):
        return True
    hit = _verdict_marker_at(head)
    if hit is None:
        return False
    marker_region = head[hit[0]:]
    # Consume the marker word itself, including its inflected tail
    # («вердикт» → «вердикта» / «вердиктов» / «вердикты» / «verdicts»).
    tail = marker_region[len(hit[1]):]
    tail = tail.lstrip("abcdefghijklmnopqrstuvwxyzабвгдеёжзийклмнопрстуфхцчшщъыьэюя")
    tail = tail.lstrip()
    if tail.startswith(":"):
        segment = _first_segment(tail[1:].strip(), _VERDICT_CONTENT_BREAKS).strip()
        return bool(_VERDICT_NEGATION_CONTENT_SEGMENT.match(segment))
    clause = _first_segment(tail, _VERDICT_CLAUSE_BREAKS).strip()
    return bool(_VERDICT_NEGATION_MARKER_CLAUSE.search(clause))


def _is_verdict_note(body: str) -> bool:
    """True when a comment OPENS as a (partial) verdict, not merely mentions it.

    Negated openings («Вердикт не вынесен», «Вердиктов пока нет», «no verdict»)
    are the inverse of a recorded verdict and must not be harvested: the forced
    close would otherwise report an item-level result for a run whose own last
    word was that it issued none (t_6c41691e D1).
    """
    text = body.strip()
    if not text:
        return False
    head = text[:_VERDICT_MARKER_WINDOW].lstrip(_VERDICT_DECORATION).lower()
    if not any(head.startswith(marker) for marker in VERDICT_NOTE_MARKERS):
        return False
    return not _verdict_is_negated(text)


_FORCED_SUMMARY_HEADER = (
    "Принудительное закрытие по мягкому порогу бюджета (soft finish): воркер "
    "израсходовал бюджет, не вызвав kanban_complete. Ниже — результат, "
    "который он успел сформировать."
)


# ── configuration ────────────────────────────────────────────────────────────


def _kanban_cfg() -> dict:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly()
    except Exception:
        return {}
    if not isinstance(cfg, dict):
        return {}
    section = cfg.get("kanban")
    return section if isinstance(section, dict) else {}


def soft_finish_enabled(kanban_cfg: Optional[dict] = None) -> bool:
    """Master switch (``kanban.soft_finish``, default on)."""
    env = os.environ.get("HERMES_KANBAN_SOFT_FINISH")
    if env is not None:
        return env.strip().lower() not in {"0", "false", "no", "off"}
    cfg = _kanban_cfg() if kanban_cfg is None else kanban_cfg
    value = cfg.get("soft_finish") if isinstance(cfg, dict) else None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return True


def soft_finish_ratio(kanban_cfg: Optional[dict] = None) -> float:
    """Trigger fraction of the run budget, clamped to a sane band."""
    cfg = _kanban_cfg() if kanban_cfg is None else kanban_cfg
    raw = cfg.get("soft_finish_ratio") if isinstance(cfg, dict) else None
    try:
        ratio = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_SOFT_FINISH_RATIO
    if ratio != ratio:  # NaN
        return DEFAULT_SOFT_FINISH_RATIO
    return min(_MAX_RATIO, max(_MIN_RATIO, ratio))


def soft_finish_grace_steps(kanban_cfg: Optional[dict] = None) -> int:
    """Steps allowed after the directive before the card is closed forcibly."""
    cfg = _kanban_cfg() if kanban_cfg is None else kanban_cfg
    raw = cfg.get("soft_finish_grace_steps") if isinstance(cfg, dict) else None
    try:
        steps = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SOFT_FINISH_GRACE_STEPS
    return max(0, steps)


def soft_finish_min_budget_steps(kanban_cfg: Optional[dict] = None) -> int:
    """Below this step budget the step arm stays inert (short routine cards)."""
    cfg = _kanban_cfg() if kanban_cfg is None else kanban_cfg
    raw = cfg.get("soft_finish_min_budget_steps") if isinstance(cfg, dict) else None
    try:
        floor = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SOFT_FINISH_MIN_BUDGET_STEPS
    return max(0, floor)


# ── budget accounting ────────────────────────────────────────────────────────


def step_budget(agent: Any) -> int:
    """The run's real step cap (``agent.max_iterations``), 0 when unlimited."""
    try:
        cap = int(getattr(agent, "max_iterations", 0) or 0)
    except (TypeError, ValueError):
        return 0
    # ``sys.maxsize`` is the core sentinel for "no cap" — not a real budget.
    if cap <= 0 or cap >= 2**62:
        return 0
    return cap


def runtime_budget() -> tuple[Optional[int], Optional[float]]:
    """``(limit_seconds, started_at_epoch)`` for this attempt, when known.

    The dispatcher exports both at spawn time. Absent values mean the card
    carries no ``max_runtime_seconds`` — the wall-clock arm stays inert and
    only the step arm can fire.
    """
    raw_limit = (os.environ.get("HERMES_KANBAN_MAX_RUNTIME_SECONDS") or "").strip()
    raw_start = (os.environ.get("HERMES_KANBAN_RUN_STARTED_AT") or "").strip()
    if not raw_limit or not raw_start:
        return None, None
    try:
        limit = int(raw_limit)
        started = float(raw_start)
    except (TypeError, ValueError):
        return None, None
    if limit <= 0 or started <= 0:
        return None, None
    return limit, started


def budget_state(agent: Any, api_call_count: int) -> dict:
    """Current consumption of both budget arms (fractions, 0..1+)."""
    cap = step_budget(agent)
    # Short routine cards: the 80% cross of a small budget is meaningless (a
    # 20-step card would be told to stop at step 16), so the step arm is inert
    # below the floor and only the wall-clock arm can arm the directive.
    floor = soft_finish_min_budget_steps()
    step_active = bool(cap) and cap > floor
    step_frac = (api_call_count / cap) if step_active else 0.0
    limit, started = runtime_budget()
    time_frac = 0.0
    elapsed = None
    if limit and started:
        elapsed = max(0.0, time.time() - started)
        time_frac = elapsed / float(limit)
    return {
        "steps_used": int(api_call_count),
        "steps_max": cap,
        "step_fraction": step_frac,
        "step_arm_active": step_active,
        "step_arm_floor": floor,
        "time_limit": limit,
        "time_elapsed": elapsed,
        "time_fraction": time_frac,
        "fraction": max(step_frac, time_frac),
    }


# ── arming ───────────────────────────────────────────────────────────────────


def soft_finish_applies(agent: Any, api_call_count: int, *, ratio: Optional[float] = None) -> bool:
    """True when this process is a live kanban worker past the soft threshold.

    Requires the same three fences as :mod:`agent.kanban_stop` (dispatcher
    envelope, dispatcher-owned context, live bound card) so an interactive
    session that inherited a stale ``HERMES_KANBAN_TASK`` can never receive
    the directive. Already-latched runs and runs that called a terminal board
    tool are excluded by the caller.
    """
    if not soft_finish_enabled():
        return False
    if not _ks.is_dispatcher_worker_env():
        return False
    if not _ks._is_owner_context():
        return False
    try:
        if not _ks.kanban_stop_nudge_enabled():
            return False
    except Exception:
        return False
    threshold = soft_finish_ratio() if ratio is None else ratio
    return budget_state(agent, api_call_count)["fraction"] >= threshold


def _terminal_tool_called(messages: Iterable[dict] | None) -> bool:
    try:
        return _ks.session_called_kanban_terminal(messages)
    except Exception:
        return False


# ── directive injection ──────────────────────────────────────────────────────


def _format_directive(state: dict) -> str:
    pct = int(round(state["fraction"] * 100))
    time_part = ""
    if state.get("time_limit") and state.get("time_elapsed") is not None:
        time_part = f", {int(state['time_elapsed'])}s/{int(state['time_limit'])}s"
    used = state["steps_used"]
    budget = state["steps_max"] or "∞"
    return SOFT_FINISH_DIRECTIVE.format(
        pct=pct, used=used, budget=budget, time_part=time_part
    )


def inject_directive(agent: Any, messages: list, api_call_count: int) -> bool:
    """Inject the one-shot soft-finish directive. Returns True when injected.

    Cache-safe delivery mirrors the run-budget wrap-up notice: the directive is
    appended to the NEWEST ``role:"tool"`` message, so no synthetic mid-loop
    user row is inserted and no past context is rewritten. Only when the
    transcript has no tool result at all (a long text-only turn) does it fall
    back to a synthetic user row, which is flagged ephemeral so it is never
    persisted to the durable transcript.
    """
    if getattr(agent, "_kanban_soft_finish_injected", False):
        return False
    state = budget_state(agent, api_call_count)
    directive = _format_directive(state)

    injected = False
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        existing = msg.get("content", "")
        if isinstance(existing, str):
            msg["content"] = existing + f"\n\n{directive}"
        else:
            try:
                blocks = list(existing) if existing else []
                blocks.append({"type": "text", "text": directive})
                msg["content"] = blocks
            except Exception:
                return False
        injected = True
        break

    if not injected:
        if messages and isinstance(messages[-1], dict) and messages[-1].get("role") == "user":
            # Never stack two user rows: the provider rejects the alternation.
            return False
        try:
            from agent.message_metadata import append_message

            append_message(
                messages,
                {
                    "role": "user",
                    "content": directive,
                    "_kanban_soft_finish_synthetic": True,
                },
            )
        except Exception:
            return False
        injected = True

    if not injected:
        return False

    agent._kanban_soft_finish_injected = True
    agent._kanban_soft_finish_at_step = int(api_call_count)
    agent._kanban_soft_finish_state = state
    logger.info(
        "kanban soft finish: directive injected at step %s/%s (%.0f%% of budget, "
        "wall=%s/%s s) task=%s",
        state["steps_used"],
        state["steps_max"] or "∞",
        state["fraction"] * 100,
        "?" if state["time_elapsed"] is None else int(state["time_elapsed"]),
        state["time_limit"] or "-",
        (os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "?",
    )
    try:
        agent._emit_status(
            f"⏳ Kanban soft finish at {int(round(state['fraction'] * 100))}% of "
            "budget — directing the worker to complete now"
        )
    except Exception:
        pass
    return True


def grace_expired(agent: Any, api_call_count: int) -> bool:
    """True when the grace window after the directive has been used up."""
    if not getattr(agent, "_kanban_soft_finish_injected", False):
        return False
    at_step = getattr(agent, "_kanban_soft_finish_at_step", None)
    if not isinstance(at_step, int):
        return True
    return (int(api_call_count) - at_step) >= soft_finish_grace_steps()


# ── forced close ─────────────────────────────────────────────────────────────


def _run_output_paths(messages: Iterable[dict] | None) -> list[str]:
    """Absolute paths of files this run wrote, de-duplicated, paths normalised.

    Shared by the summary text and the ``artifacts`` handoff so the two can
    never disagree about what the run produced.
    """
    paths: list[str] = []
    try:
        from agent.turn_finalizer import _git_status_paths, _run_file_mutation_paths

        paths = list(_run_file_mutation_paths(messages) or [])
        if not paths:
            paths = _git_status_paths(os.environ.get("HERMES_KANBAN_WORKSPACE", "").strip())
    except Exception:
        logger.debug("kanban soft finish: progress collection failed", exc_info=True)

    out: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        text = str(raw or "").strip()
        if not text:
            continue
        # ``_git_status_paths`` yields ``XY path`` lines, not bare paths.
        if not os.path.isabs(text):
            text = text[2:].strip() if len(text) > 3 and text[2] == " " else text
            workspace = os.environ.get("HERMES_KANBAN_WORKSPACE", "").strip()
            if workspace and text:
                text = os.path.join(workspace, text)
        if not text or not os.path.isabs(text):
            continue
        key = os.path.normcase(os.path.abspath(text))
        if key in seen:
            continue
        seen.add(key)
        out.append(os.path.normpath(text))
    return out


class VerdictHarvest(NamedTuple):
    """What the harvester found, split by attempt boundary.

    ``current`` — items this RUN recorded (``created_at >= run start``);
    ``inherited`` — items a PREVIOUS attempt of the same profile recorded on
    the same card. They are still this card's accumulated partial verdict, so
    dropping them would hide real work, but they must never be counted as
    "how far THIS run got" (t_6c41691e D2).
    """

    current: list
    inherited: list
    run_started_at: Optional[int]
    boundary_known: bool


def _current_run_started_at(task_id: Optional[str] = None) -> tuple[Optional[int], bool]:
    """``(started_at, known)`` for the run this process is executing.

    Source of truth is the board row for ``HERMES_KANBAN_RUN_ID`` — not the
    ``HERMES_KANBAN_RUN_STARTED_AT`` env var, which the dispatcher only exports
    when the card carries ``max_runtime_seconds`` (so a step-capped card would
    silently lose its boundary). The env value is the fallback when the run row
    cannot be read; when neither is available the boundary is UNKNOWN and the
    caller must not claim the items belong to this run.
    """
    raw_run = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    if raw_run:
        try:
            run_id = int(raw_run)
        except ValueError:
            run_id = 0
        if run_id:
            try:
                from hermes_cli import kanban_db as kb

                conn = kb.connect()
                try:
                    run = kb.get_run(conn, run_id)
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
                if run is not None:
                    started = int(getattr(run, "started_at", 0) or 0)
                    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip()
                    if started > 0 and (not tid or getattr(run, "task_id", tid) == tid):
                        return started, True
            except Exception:
                logger.debug(
                    "kanban soft finish: run start lookup failed", exc_info=True
                )
    raw_start = (os.environ.get("HERMES_KANBAN_RUN_STARTED_AT") or "").strip()
    if raw_start:
        try:
            started = int(float(raw_start))
        except (TypeError, ValueError):
            started = 0
        if started > 0:
            return started, True
    return None, False


def _run_verdict_notes(task_id: Optional[str] = None) -> list[str]:
    """``verdict:`` comments THIS RUN committed to the card, oldest first.

    Kept as a list for backwards compatibility with callers that only need the
    current attempt's items; :func:`_harvest_verdict_notes` is the full view
    (current + inherited, with the boundary state).
    """
    return list(_harvest_verdict_notes(task_id).current)


def _harvest_verdict_notes(task_id: Optional[str] = None) -> VerdictHarvest:
    """Split this profile's verdict comments by the current run's start.

    Read-only and fail-soft: a board that cannot be opened yields an empty
    harvest and the forced close behaves exactly as before. Only comments
    authored by this worker's own profile are harvested — a ``verdict:`` note
    written by another profile (a different reviewer, the orchestrator) is that
    profile's result and must not be re-attributed to this worker's handoff.

    ``task_comments`` carries no run id, so the attempt boundary is
    ``created_at >= started_at`` of THIS run. A re-dispatched card therefore
    does not inherit an earlier attempt's items into ``verdict_items_recorded``;
    those are reported separately as ``verdict_items_inherited`` and labelled
    «из прошлой попытки» in the close summary.
    """
    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not tid:
        return VerdictHarvest([], [], None, False)
    profile = (os.environ.get("HERMES_PROFILE") or "").strip()
    started, boundary_known = _current_run_started_at(tid)
    current: list[str] = []
    inherited: list[str] = []
    try:
        from hermes_cli import kanban_db as kb

        conn = kb.connect()
    except Exception:
        logger.debug("kanban soft finish: verdict notes — board connection failed", exc_info=True)
        return VerdictHarvest([], [], started, boundary_known)
    try:
        for comment in kb.list_comments(conn, tid):
            body = str(getattr(comment, "body", "") or "").strip()
            author = str(getattr(comment, "author", "") or "").strip()
            if profile and author and author != profile:
                continue
            if not _is_verdict_note(body):
                continue
            if body.lower().startswith(VERDICT_NOTE_PREFIX):
                note = body[len(VERDICT_NOTE_PREFIX):].strip() or body
            else:
                note = body
            # Without a known boundary, a comment cannot be attributed to this
            # run — it goes to ``inherited`` so the counter stays honest.
            if boundary_known and int(getattr(comment, "created_at", 0) or 0) >= started:
                current.append(note)
            else:
                inherited.append(note)
    except Exception:
        logger.debug("kanban soft finish: verdict notes read failed", exc_info=True)
        return VerdictHarvest([], [], started, boundary_known)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return VerdictHarvest(current, inherited, started, boundary_known)


_VERDICT_NOTE_INHERITED_HEADER = (
    "Пункты вердикта из ПРОШЛОЙ попытки этой же карточки "
    "(не результат текущего прогона):"
)


def _progress_summary(
    messages: Iterable[dict] | None,
    paths: Optional[list[str]] = None,
    verdict_notes: Optional[list[str]] = None,
    inherited_notes: Optional[list[str]] = None,
) -> str:
    """Best available description of what the worker already produced.

    Reuses ``turn_finalizer``'s deterministic collector (no LLM, no network):
    the last visible narration plus the files the run actually touched. When
    the run left no trace the honest answer is the header alone — an empty
    summary would hide the forced close from the board.

    ``verdict_notes`` are the ``verdict:`` comments THIS run committed to the
    board as it decided each item. They lead the summary because they are the
    only part of the result that survived the run: the review's item-level
    findings are exactly what a reviewer card exists to deliver, and they are
    absent from the narration tail whenever the worker was still mid-check
    (t_417a182e — 156/180 steps spent re-verifying, no verdict issued).

    ``inherited_notes`` belong to an EARLIER attempt of the same card. They are
    shown, because dropping a previous attempt's decided items would lose real
    work, but under their own header and never as «how far this run got»
    (t_6c41691e D2).
    """
    tail = ""
    try:
        from agent.turn_finalizer import _last_assistant_text

        tail = _last_assistant_text(messages) or ""
    except Exception:
        logger.debug("kanban soft finish: progress collection failed", exc_info=True)

    if paths is None:
        paths = _run_output_paths(messages)
    if verdict_notes is None and inherited_notes is None:
        harvest = _harvest_verdict_notes()
        verdict_notes = harvest.current
        inherited_notes = harvest.inherited
    verdict_notes = verdict_notes or []
    inherited_notes = inherited_notes or []

    parts = [_FORCED_SUMMARY_HEADER]

    def _render(notes: list[str]) -> str:
        # A harvested note may be a whole verdict body (an in-the-worker's-words
        # partial verdict is accepted, not only the one-line `verdict:` form).
        # Keep each one readable in the close: first line(s) up to a fixed cap,
        # because the full text already lives in the board comment it came from.
        shown = []
        for note in notes[:40]:
            text = note.strip()
            if len(text) > _VERDICT_SUMMARY_NOTE_MAX_CHARS:
                text = text[:_VERDICT_SUMMARY_NOTE_MAX_CHARS].rstrip() + " …"
            shown.append(text)
        return "\n".join(f"- {n}" for n in shown)

    if verdict_notes:
        parts.append(_VERDICT_NOTE_HEADER + "\n" + _render(verdict_notes))
    if inherited_notes:
        parts.append(_VERDICT_NOTE_INHERITED_HEADER + "\n" + _render(inherited_notes))
    if tail:
        parts.append("Последнее, что воркер сообщил:\n" + tail)
    if paths:
        parts.append("Файлы, созданные/изменённые в этом прогоне:\n" + "\n".join(f"- {p}" for p in paths[:40]))
    return "\n\n".join(parts)


def force_complete(
    *,
    summary_extra: str = "",
    reason: str = "soft_finish_grace_expired",
    messages: Iterable[dict] | None = None,
    budget: Optional[dict] = None,
) -> bool:
    """Close this worker's card ``done`` with the progress it already has.

    Fail-closed on identity: the card must still be ``running`` AND bound to
    this exact run/profile/session (``has_authenticated_worker_binding``)
    before any write. A blocked/completed card, a reclaimed run, or a stale
    inherited env returns ``False`` and the caller keeps its normal path.
    """
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    raw_run = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    profile = (os.environ.get("HERMES_PROFILE") or "").strip()
    session_id = (os.environ.get("HERMES_SESSION_ID") or "").strip()
    if not task_id or not raw_run or not profile:
        return False
    try:
        run_id = int(raw_run)
    except ValueError:
        return False

    # The summary records EVERY path the run touched (honest audit trail,
    # including files that have since vanished); ``artifacts`` declares only
    # what can actually be preserved — a declared-but-absent path raises
    # ArtifactPreservationError in ``complete_task`` and would abort the whole
    # forced close, losing the handoff.
    touched = _run_output_paths(messages)
    artifacts = [p for p in touched if os.path.isfile(p)]
    harvest = _harvest_verdict_notes()
    verdict_notes = list(harvest.current)
    inherited_notes = list(harvest.inherited)

    summary = _progress_summary(
        messages,
        paths=touched,
        verdict_notes=verdict_notes,
        inherited_notes=inherited_notes,
    )
    if summary_extra:
        summary = summary + "\n\n" + summary_extra.strip()

    metadata = {
        "soft_finish_forced": True,
        "forced_close_reason": reason,
        # Distinguish «the run was cut short» from «the task is verified done»
        # (t_417a182e). The directive now asks a review worker to commit each
        # decided item to the board as it decides it, so this counter is the
        # durable measure of how far it actually got: 0 means the close carries
        # narration only and the card's real deliverable (an item-level
        # verdict) was never issued.
        #
        # Only THIS run's items are counted (t_6c41691e D2): ``task_comments``
        # carries no run id, so a re-dispatched card's earlier attempt would
        # otherwise inflate the count and close a run that decided nothing with
        # a non-zero score. Those are reported separately below so the earlier
        # work is preserved without being mis-attributed.
        "verdict_items_recorded": len(verdict_notes),
        "verdict_items_inherited": len(inherited_notes),
        "verdict_partial": True,
    }
    # Surface the boundary state in the handoff: with an unknown run start the
    # items cannot be attributed to this attempt at all, and a downstream reader
    # must not read ``verdict_items_recorded: 0`` as «the run decided nothing».
    metadata["verdict_boundary_known"] = bool(harvest.boundary_known)
    if harvest.run_started_at is not None:
        metadata["verdict_run_started_at"] = int(harvest.run_started_at)
    # Declare the run's own output as artifacts explicitly (t_24120021).
    # Previously the forced close carried the file list as PROSE only, so
    # saving the work depended on the completion-prose scanner re-parsing it.
    # That made the last line of defence a string-format detail: a worker whose
    # paths were written with forward slashes lost every file to
    # ``_cleanup_workspace``.  Passing ``artifacts`` here drives the documented
    # path (``_persist_scratch_completion_artifacts`` copies each file into the
    # task attachment store before the scratch dir is removed).
    if artifacts:
        metadata["artifacts"] = artifacts

    if isinstance(budget, dict):
        metadata.update(
            {
                "budget_used": budget.get("steps_used"),
                "budget_max": budget.get("steps_max"),
                "budget_fraction": round(float(budget.get("fraction") or 0.0), 4),
                "time_elapsed_seconds": (
                    None if budget.get("time_elapsed") is None else int(budget["time_elapsed"])
                ),
                "time_limit_seconds": budget.get("time_limit"),
            }
        )

    try:
        from hermes_cli import kanban_db as kb

        conn = kb.connect()
    except Exception:
        logger.warning("kanban soft finish: board connection failed", exc_info=True)
        return False
    try:
        if not kb.has_authenticated_worker_binding(conn, task_id, run_id, profile, session_id):
            logger.info(
                "kanban soft finish: forced close skipped — binding gone (task=%s run=%s)",
                task_id, run_id,
            )
            return False
        ok = kb.complete_task(
            conn,
            task_id,
            summary=summary,
            metadata=metadata,
            expected_run_id=run_id,
        )
    except Exception:
        logger.warning("kanban soft finish: forced close failed", exc_info=True)
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if ok:
        logger.warning(
            "kanban soft finish: card %s closed done forcibly (%s) with the "
            "progress the worker had — prevented a timed_out",
            task_id, reason,
        )
    return bool(ok)


__all__ = [
    "DEFAULT_SOFT_FINISH_GRACE_STEPS",
    "DEFAULT_SOFT_FINISH_MIN_BUDGET_STEPS",
    "DEFAULT_SOFT_FINISH_RATIO",
    "SOFT_FINISH_DIRECTIVE",
    "VERDICT_NOTE_PREFIX",
    "VERDICT_NOTE_MARKERS",
    "budget_state",
    "force_complete",
    "grace_expired",
    "inject_directive",
    "runtime_budget",
    "soft_finish_applies",
    "soft_finish_enabled",
    "soft_finish_grace_steps",
    "soft_finish_min_budget_steps",
    "soft_finish_ratio",
    "step_budget",
]
