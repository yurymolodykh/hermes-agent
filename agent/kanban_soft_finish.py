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
import time
from typing import Any, Iterable, Optional

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
    "Your very next action must be `kanban_complete(summary=..., "
    "artifacts=[...])` using the result you already have — even if the "
    "remaining polish is unfinished. A card that finishes with an honest "
    "partial result is accepted; a card that runs out of budget is recorded "
    "as `timed_out` and its work is at risk.\n"
    "If the work genuinely cannot be completed, call `kanban_block(reason=...)` "
    "instead — but do not spend another step exploring."
)

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


def _progress_summary(messages: Iterable[dict] | None) -> str:
    """Best available description of what the worker already produced.

    Reuses ``turn_finalizer``'s deterministic collector (no LLM, no network):
    the last visible narration plus the files the run actually touched. When
    the run left no trace the honest answer is the header alone — an empty
    summary would hide the forced close from the board.
    """
    tail = ""
    paths: list[str] = []
    try:
        from agent.turn_finalizer import _git_status_paths, _last_assistant_text, _run_file_mutation_paths

        tail = _last_assistant_text(messages) or ""
        paths = list(_run_file_mutation_paths(messages) or [])
        if not paths:
            paths = _git_status_paths(os.environ.get("HERMES_KANBAN_WORKSPACE", "").strip())
    except Exception:
        logger.debug("kanban soft finish: progress collection failed", exc_info=True)

    parts = [_FORCED_SUMMARY_HEADER]
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

    summary = _progress_summary(messages)
    if summary_extra:
        summary = summary + "\n\n" + summary_extra.strip()

    metadata = {
        "soft_finish_forced": True,
        "forced_close_reason": reason,
    }
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
