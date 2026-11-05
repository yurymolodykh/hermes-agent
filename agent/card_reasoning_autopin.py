"""Rule-based autopin of a card's reasoning level (t_d7edc3b2).

Owner requirement (carried over from the cancelled ``t_bdcbc547``): an ACTIVE
card (``triage``/``todo``/``ready``/``running``/``blocked``) must never inherit
its assignee profile's ``agent.reasoning_effort`` — it always owns an explicit
``tasks.reasoning_effort``. ``t_f2dd1fb5`` already guarantees that at CREATE
time (an absent pin is stored as the card default ``high``, or ``max`` for a
review-gate assignee) and at SPAWN time (a legacy NULL row is substituted).
What it does not do is choose the level by the owner's RULES, nor clean up the
legacy rows that predate it.

This module is that missing piece:

* :func:`resolve_card_autopin_effort` — the pure rule resolver (no I/O);
* :func:`autopin_active_cards` — one idempotent sweep over the board that
  writes the resolved level onto every active card that does not already own a
  deliberate one, and records an audit event per change.

Rule order (owner's list, first match wins — `SOUL`/card body `t_d7edc3b2`):

1. ``09-independent-reviewer`` / ``15-sre-qa`` → ``max`` (quality gate).
2. A card whose direct child is a review-gate profile (the "container whose
   only/main child is the reviewer" shape and the "author card with a review
   child" shape both land here) → ``high``.
3. РГЗ mission cards (``РГЗ``/``RGZ`` in the title, or ``goal_mode``):
   mission container / phase judgement-review (разбор, КРЗ, судейство,
   приёмка фазы) → ``max``; wave control (сбор статусов, NIGHT-PLAN, digest,
   «доведи волну») → ``high``; role not determined unambiguously → ``high``.
4. Mechanical routine → ``low``.
5. Anything else → ``high``.

Every level is inside the letovo wire domain ``{none, low, high, xhigh, max}``.
``medium``/``minimal``/``ultra`` are NEVER produced (the endpoint answers HTTP
400 on them — measured 2026-10-30, see ``agent/reasoning_effort_l1``).

Deliberately conservative about existing values: a sweep only writes when the
card has no level (NULL), carries a level outside the served domain, or still
carries the plain pre-rule card default while no operator pin event exists.
An explicit operator pin — including ``none`` — is never overwritten.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

_log = logging.getLogger(__name__)

__all__ = [
    "ACTIVE_AUTOPIN_STATUSES",
    "AUTO_EFFORT_PIN",
    "LETOVO_WIRE_DOMAIN",
    "resolve_auto_effort",
    "resolve_card_autopin_effort",
    "autopin_active_cards",
]

#: Statuses the owner calls ACTIVE — every one of them must own a level.
ACTIVE_AUTOPIN_STATUSES: tuple[str, ...] = (
    "triage",
    "todo",
    "ready",
    "running",
    "blocked",
)

#: The letovo wire domain, repeated here so this module needs no config read
#: for its own validation (kept in sync by
#: ``tests/agent/test_card_reasoning_autopin.py``).
LETOVO_WIRE_DOMAIN: tuple[str, ...] = ("none", "low", "high", "xhigh", "max")

#: The ``auto`` sentinel a card may be PINNED to (t_f89294b8). ``auto`` is not
#: a wire level: it names a RULE, not a depth. The dispatcher resolves it at
#: SPAWN time from the card's own shape, so the worker always receives a
#: concrete ``--reasoning <level>`` and the pin survives shape changes (a card
#: that gains children flips to the authoring level on its next spawn).
AUTO_EFFORT_PIN = "auto"

#: Profiles that author work and own the board's decomposition/steering lane.
#: A card assigned to one of them is an authoring/container card by definition
#: — the owner's «постановка карточек» shape.
_AUTHORING_ASSIGNEES = frozenset({"01-orchestrator"})

#: Fallback when ``agent.reasoning_effort_l1`` cannot be imported (a stripped
#: install): the same two profiles that own the independent review gate.
_REVIEWER_FALLBACK = frozenset({"09-independent-reviewer", "15-sre-qa"})


def _reviewer_profiles() -> frozenset:
    try:
        from agent.reasoning_effort_l1 import REVIEWER_PROFILES

        return frozenset(REVIEWER_PROFILES)
    except Exception:  # pragma: no cover - defensive (stripped install)
        return _REVIEWER_FALLBACK


def _is_rgz_title(title: Optional[str]) -> bool:
    """``РГЗ``/``RGZ`` marker anywhere in the title (Cyrillic-safe lower())."""
    return bool(title) and (
        "ргз" in str(title).lower() or "rgz" in str(title).lower()
    )


#: Phase-judgement / adjudication markers inside a РГЗ mission. A card matching
#: one of these decides something (a разбор, a КРЗ, a phase acceptance), so it
#: sits on the top served level.
_RGZ_JUDGEMENT_MARKERS: tuple[str, ...] = (
    "разбор",
    "крз",
    "судейств",
    "судейск",
    "приёмк",
    "приемк",
    "аудит фаз",
    "заключение по фазе",
)

#: Mission-container markers: the card that launches / conceives the mission.
_RGZ_MISSION_MARKERS: tuple[str, ...] = (
    "запуск",
    "замысел",
    "мисси",
    "контейнер",
    "план миссии",
)

#: Wave-control markers: operational steering, not judgement (owner: ``high``).
_RGZ_WAVE_MARKERS: tuple[str, ...] = (
    "сбор статус",
    "night-plan",
    "night plan",
    "дайджест",
    "digest",
    "доведи волну",
    "контроль волн",
    "статус волн",
)

#: Unambiguously MECHANICAL routine work → ``low``. Deliberately narrow: a
#: substring match here DOWNGRADES cost, so only work with no engineering
#: judgement may appear. Anything merely "short" stays on ``high``.
_ROUTINE_MARKERS: tuple[str, ...] = (
    "commit-drift",
    "расчистк",
    "автоудален",
    "автоудаля",
    "форматируй",
    "переведи",
    "перевод ",
    "дайджест",
    "digest",
    "сбор статус",
    "night-plan",
    "справк",
    "напоминани",
    "нарезк",
    "ярлык",
)

#: Markers that make a card NON-routine even when a routine marker matched —
#: one of these wins and the level stays ``high``. Keeps the downgrade honest.
_COMPLEX_MARKERS: tuple[str, ...] = (
    "чинить",
    "починить",
    "fix",
    "исправ",
    "реализу",
    "реализ",
    "спроект",
    "архитектур",
    "рефактор",
    "диагност",
    "разобраться",
    "разбор",
    "аудит",
    "ревью",
    "причин",
    "root cause",
    "регресс",
    "race",
    "утечк",
    "миграци",
    "оптимизац",
    "повысить",
    "снизить",
    "ускор",
    "надёжн",
    "надежн",
)


def _has_any(hay: str, markers: Iterable[str]) -> bool:
    return any(m in hay for m in markers)


def resolve_auto_effort(
    *,
    assignee: Optional[str],
    title: Optional[str] = None,
    goal_mode: bool = False,
    child_count: int = 0,
    planning_node: bool = False,
) -> tuple[str, str]:
    """Resolve the ``auto`` pin into a concrete level for one card (t_f89294b8).

    Pure: no DB, no config, no I/O. The caller supplies the card's DIRECT
    child count (``task_links.parent_id = id``) because that is the only
    shape signal the DB owns.

    Strict order (first match wins — the card body's list):

    1. ``09-independent-reviewer`` / ``15-sre-qa`` → ``max``. Independent
       acceptance is the quality gate; it is the most expensive error.
    2. A container / authoring card — ANY of:
       * ``assignee == 01-orchestrator`` (the posting/decomposition lane),
       * **``planning_node`` is on** (t_cabee680) — the author DECLARED this
         card a planning node, so it is one even before it posts a single
         child,
       * it owns at least one child (``child_count >= 1``),
       * ``goal_mode`` is on (a goal-loop worker re-plans its own turns),
       * the РГЗ trigger fired (the SAME phrase list as the fast lane —
         ``agent.rgz_reasoning.rgz_trigger_detected``, no second list)
       → ``max``.
    3. Everything else → ``low``.

    ``max`` and ``low`` both live inside the letovo wire domain, so a level
    produced here can never 400 on the current endpoint. ``medium`` /
    ``minimal`` / ``ultra`` are NEVER produced.

    Degenerate input never raises: an empty/unknown assignee, a NULL title or
    a negative child count simply falls through to ``low``.

    ORDERING (t_cabee680): the ``child_count`` signal alone is read at SPAWN
    time, so a card that posts its children only later would look like a leaf
    on its first spawn. ``planning_node`` exists precisely to remove that
    dependence: it is written when the card is CREATED (or latched the first
    time the card is observed with children), so the node property no longer
    decays with the order in which the chain happens to be executed — and it
    survives the children's completion, which ``child_count`` does not.
    ``child_count`` stays as an inference for cards nobody declared.
    """
    name = str(assignee or "").strip().lower()
    text = str(title or "").strip()

    # 1. Independent review gate.
    if name and name in _reviewer_profiles():
        return "max", "auto:review-gate-assignee"

    # 2. Container / authoring card. ``planning_node`` comes FIRST among the
    # shape signals: it is the only one that is true before the children exist
    # and stays true after they close (t_cabee680).
    if name and name in _AUTHORING_ASSIGNEES:
        return "max", "auto:authoring-assignee"
    if planning_node:
        return "max", "auto:planning-node"
    if int(child_count or 0) > 0:
        return "max", "auto:container-with-children"
    if goal_mode:
        return "max", "auto:goal-mode"
    if _rgz_trigger(text):
        return "max", "auto:rgz-trigger"

    # 3. Everything else — the mechanical/execution lane.
    return "low", "auto:execution"


def _rgz_trigger(text: Optional[str]) -> bool:
    """True when *text* fires the single shared РГЗ phrase contract.

    Reuses ``agent.rgz_reasoning.rgz_trigger_detected`` (which in turn reuses
    the fast-lane «always-slow» list) so there is exactly ONE phrase list in
    the codebase. Falls back to the same regex-level check when that module
    cannot be imported (stripped install), still without a second list.
    """
    if not text:
        return False
    try:
        from agent.rgz_reasoning import rgz_trigger_detected

        return bool(rgz_trigger_detected(text))
    except Exception:  # pragma: no cover - defensive (stripped install)
        try:
            from agent.fast_lane import contains_always_slow_phrase

            return bool(contains_always_slow_phrase(text))
        except Exception:
            return False


def resolve_card_autopin_effort(
    *,
    assignee: Optional[str],
    title: Optional[str] = None,
    goal_mode: bool = False,
    priority: Optional[int] = None,
    has_reviewer_child: bool = False,
    child_count: int = 0,
    planning_node: bool = False,
) -> tuple[str, str]:
    """Resolve ``(level, reason)`` for one card by the owner's rules.

    Pure: no DB, no config, no I/O. ``has_reviewer_child`` / ``child_count``
    describe the card's DIRECT children (``child_count`` is only used to tell
    the "container" shape from the "author with a review child" shape for the
    audit reason — both resolve to ``high``). ``planning_node`` is the
    author-declared (or latched) "this card posts children" flag of
    t_cabee680; a declared node is a POSITION on ``max`` regardless of how many
    children exist right now.

    The returned level is always inside :data:`LETOVO_WIRE_DOMAIN`.
    """
    name = str(assignee or "").strip().lower()
    text = f"{title or ''}".strip()
    hay = text.lower()

    # 1. Independent review gate — the top served level.
    if name and name in _reviewer_profiles():
        return "max", "review-gate-assignee"

    # 2. Declared/latched planning node (t_cabee680). Sweep-side counterpart of
    # ``auto:planning-node``: a card that posts other cards is a mission
    # container shape, so it sits on max before (and after) its children exist.
    if planning_node:
        return "max", "planning-node"

    # 3. Gated work: the card owns a review-gate child.
    if has_reviewer_child:
        reason = (
            "review-child-container" if int(child_count or 0) <= 1
            else "author-with-review-child"
        )
        return "high", reason

    # 3. РГЗ (giant-mission) class.
    is_rgz = _is_rgz_title(text) or bool(goal_mode)
    if is_rgz:
        if _has_any(hay, _RGZ_JUDGEMENT_MARKERS):
            return "max", "rgz-phase-judgement"
        if _has_any(hay, _RGZ_MISSION_MARKERS):
            return "max", "rgz-mission-container"
        if priority is not None and int(priority) < 0:
            # The owner's mission-container signature includes a negative
            # priority (the launch/intent card above all waves).
            return "max", "rgz-mission-container"
        if _has_any(hay, _RGZ_WAVE_MARKERS):
            return "high", "rgz-wave-control"
        return "high", "rgz-role-ambiguous"

    # 4. Mechanical routine — only when nothing marks the card as real work.
    if _has_any(hay, _ROUTINE_MARKERS) and not _has_any(hay, _COMPLEX_MARKERS):
        return "low", "mechanical-routine"

    # 5. Everything else (incl. an unclassifiable role) stays on the served
    # middle: never fall below the level gated work needs.
    return "high", "default-uncertain"


def legacy_card_default(assignee: Optional[str]) -> str:
    """The plain pre-rule card default for *assignee* (t_f2dd1fb5).

    ``max`` for a review-gate profile, ``high`` otherwise. A stored level that
    equals this value is the ONE case a sweep may correct: it proves the row was
    written by the card default, not by an operator.
    """
    name = str(assignee or "").strip().lower()
    if name and name in _reviewer_profiles():
        return "max"
    return "high"


def _clamp_to_domain(level: Optional[str]) -> Optional[str]:
    """Map an out-of-domain level onto the served domain (never ``None``)."""
    try:
        from agent.reasoning_effort_l1 import clamp_to_letovo

        return clamp_to_letovo(level)
    except Exception:  # pragma: no cover - defensive (stripped install)
        value = str(level or "").strip().lower()
        return value if value in LETOVO_WIRE_DOMAIN else None


def _provenance(conn: Any, task_id: str) -> str:
    """Who last wrote this card's level: ``"operator"``, ``"autopin"`` or ``""``.

    Provenance is read from the event trail, not guessed from the value:

    * ``reasoning_effort_set`` with a non-NULL ``reasoning_effort_stored`` — the
      CLI/dashboard path (``set_reasoning_effort``) — is an OPERATOR decision;
    * ``reasoning_effort_autopin`` — written by this module (creation default or
      sweep) — is RULE-OWNED and may be re-derived when the card's shape
      changes (e.g. it gains a review child);
    * no event at all — a legacy row of unknown origin, handled conservatively
      by the caller.

    The most recent relevant event wins, so an operator pin recorded after an
    autopin keeps the card pinned the other way round.
    """
    try:
        rows = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind IN "
            "('reasoning_effort_set', 'reasoning_effort_autopin') "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    except Exception:  # pragma: no cover - defensive
        return ""
    if not rows:
        return ""
    kind = rows["kind"] if hasattr(rows, "keys") else rows[0]
    raw = rows["payload"] if hasattr(rows, "keys") else rows[1]
    if kind == "reasoning_effort_autopin":
        return "autopin"
    if not raw:
        return ""
    import json

    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):  # pragma: no cover - malformed payload
        return ""
    if not isinstance(payload, dict):
        return ""
    # A CLEARED pin (stored null) leaves the card unpinned — not an operator
    # level decision, so it must not block the rule.
    return "operator" if payload.get("reasoning_effort_stored") else ""


def _operator_pinned(conn: Any, task_id: str) -> bool:
    """True when an operator/dashboard pin is on record for *task_id*."""
    return _provenance(conn, task_id) == "operator"


def _graph_of(conn: Any, task_id: str, reviewer_names: frozenset) -> tuple[bool, int]:
    """``(has_reviewer_child, child_count)`` for one card's DIRECT children."""
    try:
        rows = conn.execute(
            "SELECT t.assignee FROM task_links l "
            "LEFT JOIN tasks t ON t.id = l.child_id "
            "WHERE l.parent_id = ?",
            (task_id,),
        ).fetchall()
    except Exception:  # pragma: no cover - defensive
        return False, 0
    count = 0
    has_reviewer = False
    for row in rows:
        count += 1
        child_assignee = row["assignee"] if hasattr(row, "keys") else row[0]
        name = str(child_assignee or "").strip().lower()
        if name and name in reviewer_names:
            has_reviewer = True
    return has_reviewer, count


def autopin_active_cards(
    conn: Any,
    *,
    dry_run: bool = False,
    limit: Optional[int] = None,
    task_ids: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Give every ACTIVE card an explicit, rule-derived reasoning level.

    Idempotent. Returns a report::

        {
          "checked": int,          # active cards examined
          "changed": [(id, old, new, reason), ...],
          "unchanged": int,
          "skipped_pinned": [(id, level), ...],   # operator pins respected
          "dry_run": bool,
        }

    A card is written when its stored level is NULL, outside the served domain,
    or still the plain pre-rule card default with no operator pin on record. An
    explicit operator pin (including ``none``) is never overwritten.
    """
    from hermes_cli import kanban_db as kb

    reviewer_names = _reviewer_profiles()
    if task_ids is not None:
        wanted = [str(t) for t in task_ids]
    else:
        wanted = None

    if wanted is not None:
        placeholders = ",".join("?" for _ in wanted)
        query = (
            "SELECT id, title, assignee, status, goal_mode, "
            "planning_node, priority, reasoning_effort FROM tasks "
            f"WHERE id IN ({placeholders})"
        )
        args: tuple = tuple(wanted)
    else:
        placeholders = ",".join("?" for _ in ACTIVE_AUTOPIN_STATUSES)
        query = (
            "SELECT id, title, assignee, status, goal_mode, "
            "planning_node, priority, reasoning_effort FROM tasks "
            f"WHERE status IN ({placeholders}) "
            "ORDER BY priority DESC, created_at ASC"
        )
        args = tuple(ACTIVE_AUTOPIN_STATUSES)

    rows = conn.execute(query, args).fetchall()
    if limit is not None:
        rows = rows[: max(0, int(limit))]

    checked = 0
    changed: list[tuple[str, Optional[str], str, str]] = []
    skipped_pinned: list[tuple[str, str]] = []
    unchanged = 0

    for row in rows:
        keys = row.keys() if hasattr(row, "keys") else ()
        get = (lambda k: row[k]) if keys else None
        task_id = get("id") if get else row[0]
        title = get("title") if get else row[1]
        assignee = get("assignee") if get else row[2]
        status = get("status") if get else row[3]
        goal_mode = get("goal_mode") if get else row[4]
        planning_node = get("planning_node") if get else row[5]
        priority = get("priority") if get else row[6]
        stored = get("reasoning_effort") if get else row[7]

        if wanted is not None and status not in ACTIVE_AUTOPIN_STATUSES:
            continue
        checked += 1

        has_reviewer_child, child_count = _graph_of(conn, task_id, reviewer_names)
        desired, reason = resolve_card_autopin_effort(
            assignee=assignee,
            title=title,
            goal_mode=bool(goal_mode),
            priority=priority,
            has_reviewer_child=has_reviewer_child,
            child_count=child_count,
            planning_node=bool(planning_node),
        )

        stored_level = str(stored).strip().lower() if stored else None
        origin = _provenance(conn, str(task_id))

        if stored_level == AUTO_EFFORT_PIN:
            # ``auto`` is a deliberate RULE choice, not a missing level
            # (t_f89294b8): the dispatcher resolves it per spawn, so the sweep
            # must never replace the sentinel with a frozen level — doing so
            # would silently turn a self-updating pin into a one-shot decision
            # and break the "container that gains children flips to max" case.
            skipped_pinned.append((str(task_id), AUTO_EFFORT_PIN))
            unchanged += 1
            continue

        if stored_level is None:
            # No level at all: the rule decides (the legacy-row case).
            write_level, write_reason = desired, reason
        elif origin == "operator":
            # An explicit operator choice. Respect it — only repair a value the
            # wire would 400 on, and then keep its DIRECTION (medium->high,
            # minimal->low) rather than overriding the operator with a rule.
            clamped = _clamp_to_domain(stored_level)
            if clamped is not None and clamped != stored_level:
                changed.append(
                    (str(task_id), stored_level, clamped, "domain-clamp")
                )
                continue
            if stored_level != desired:
                skipped_pinned.append((str(task_id), stored_level))
            unchanged += 1
            continue
        elif origin == "autopin":
            # Rule-owned: re-derive freely when the card's shape changed.
            if stored_level == desired:
                unchanged += 1
                continue
            write_level, write_reason = desired, reason
        elif stored_level not in LETOVO_WIRE_DOMAIN:
            # Out-of-domain and of UNKNOWN origin (a legacy/auto write): the
            # rule owns it. Never emit a level the wire rejects.
            write_level, write_reason = desired, f"{reason}+domain-repair"
        elif stored_level != legacy_card_default(assignee):
            # A deliberate non-default level with no pin event: leave it alone
            # (legacy hand-set values such as ``none``/``xhigh`` are choices).
            unchanged += 1
            continue
        elif stored_level == desired:
            unchanged += 1
            continue
        else:
            write_level, write_reason = desired, reason

        changed.append((str(task_id), stored_level, write_level, write_reason))

    report = {
        "checked": checked,
        "changed": changed,
        "unchanged": unchanged,
        "skipped_pinned": skipped_pinned,
        "dry_run": bool(dry_run),
    }
    if dry_run or not changed:
        return report

    # Write in ONE transaction, then notify once per changed card AFTER commit.
    with kb.write_txn(conn):
        for task_id, old_level, new_level, reason in changed:
            conn.execute(
                "UPDATE tasks SET reasoning_effort = ? WHERE id = ?",
                (new_level, task_id),
            )
            # A domain-repair of an OPERATOR pin must stay operator-owned: if it
            # were stamped ``reasoning_effort_autopin`` the next sweep would
            # treat the value as rule-owned and could re-derive it away.
            kb._append_event(
                conn,
                task_id,
                "reasoning_effort_set"
                if reason == "domain-clamp"
                else "reasoning_effort_autopin",
                {
                    "from": old_level,
                    "to": new_level,
                    "reason": reason,
                    "by": "card-autopin",
                    **(
                        {
                            "reasoning_effort": new_level,
                            "reasoning_effort_stored": new_level,
                        }
                        if reason == "domain-clamp"
                        else {}
                    ),
                },
            )
    for task_id, _old, _new, _reason in changed:
        try:
            kb.notify_task_updated(conn, task_id, ("reasoning_effort",))
        except Exception:  # pragma: no cover - observer is best-effort
            _log.debug("task-updated notify failed for %s", task_id, exc_info=True)
    _log.info(
        "card autopin: %d active card(s) checked, %d pinned, %d operator pin(s) respected",
        checked,
        len(changed),
        len(skipped_pinned),
    )
    return report
