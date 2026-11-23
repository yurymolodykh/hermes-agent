"""Card-sizing rule: does a card's declared volume fit the assignee's *two* budgets?

Two independent limits stop a run, and they are not the same axis:

* the assignee profile's iteration budget (``agent.max_turns``) — the rule's original
  axis. An iteration budget belongs to the *assignee profile*, not to the card, and a
  card that cannot fit does not fail loudly — it burns its last iteration mid-edit and
  stops (``t_f3d28e37``: 1459 verified lines, uncommitted);
* the card's own wall-clock cap (``task_runs.max_runtime_seconds``, written from
  ``hermes_cli.kanban_db.DEFAULT_MAX_RUNTIME_SECONDS`` = 3600 s at creation). This is
  what actually kills runs on this board: on 23.11.2026 two cards went ``timed_out``
  at that wall while the sizing rule stayed silent — four acceptance items estimated
  at 160 iterations against a 240 budget ("fits") and 36 minutes against a 60-minute
  wall ("fits"), and the run died at the wall it had been told it would fit.

The iteration axis is deliberately coarse; the minute axis is calibrated on the board's
own history rather than on a linear guess. Both are stated in work items the card
already names: its acceptance criteria.

Measured on the shared board (``task_runs`` joined to ``tasks``, duration =
``ended_at − started_at``, runs with a usable duration, 17–23.11.2026: 763 runs,
percentiles by nearest rank on the sorted durations):

===========  =======  ======  ======  ==========
acceptance   runs     p50     p80     share > 60
items       (n)       min     min     min
===========  =======  ======  ======  ==========
0            223      23      61      21 %
3            52       40      81      29 %
4            233      42      79      30 %
5            170      46      90      36 %
6            54       52      91      48 %
===========  =======  ======  ======  ==========

Above six items the figure is held at the six-item point. The 7+ bucket (27 runs)
measures *lower* — p50 38, p80 75 — because those cards are mostly fan-out parents
whose wall clock is orchestration, not work; letting that dip through would predict a
bigger card as faster. Two items has four runs and is interpolated, not tabulated.

The whole sample: median 38 min, p80 76 min, 30 % of runs longer than 60 min, 40 %
longer than 50 min. The old ``12 + 6 × items`` line put four items at 36 min — a 2.2×
under-estimate against the measured p80 for that bucket (79 min), which is why the
minute axis never fired.

Budget stops are NOT mostly a size problem: 25 of the 31 original budget stops carried
a normal-looking card. Neither axis alone is the variable — which is why the rule warns
rather than blocks, and why the WIP checkpoint (``agent.kanban_budget_checkpoint``), not
the split, is what keeps the work.

The rule is advisory by construction: it never blocks ``kanban_create``. A card may
legitimately be bigger than the profile and be split by its creator.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

# Iterations one acceptance criterion plausibly costs: plan the change, make it,
# run its probe, write it down. Fitted against the board's finished runs.
ITERATIONS_PER_CRITERION = 30
# Fixed overhead of any card: orient, read the workspace, hand off.
BASE_ITERATIONS = 40
# Head-room for the model's own bookkeeping turns (comments, heartbeats, greps).
SAFETY_MARGIN = 1.0

# Wall-clock calibration: minutes per acceptance-item count, measured on the shared
# board (see the module docstring for the query shape and the sample). Keys are item
# counts that the board actually has enough runs for; values are the measured
# percentiles, rounded to whole minutes. Interpolated linearly between points and
# clamped to the last point above it — no point is extrapolated from a formula.
#
# Source: ``task_runs`` JOIN ``tasks`` on the shared board, ``ended_at − started_at``
# in seconds, runs with a usable duration, 17–23.11.2026 (763 runs), acceptance items
# counted by ``count_acceptance_items`` on the task body, percentiles by nearest rank.
# Recheck with ``tests/agent/test_kanban_card_sizing.py``'s ``CALIBRATION_*``
# round-trip; the docstring table is the same measurement.
CALIBRATION_P50_MINUTES = {0: 23, 3: 40, 4: 42, 5: 46, 6: 52}
CALIBRATION_P80_MINUTES = {0: 61, 3: 81, 4: 79, 5: 90, 6: 91}
# The owner's target for one run: a card is authored to close in 30–50 minutes.
CARD_MINUTE_TARGET = 50
# Wall-clock cap written into a card that names none (``hermes_cli.kanban_db``'s own
# default). Read lazily so this module keeps importing without the CLI package.
FALLBACK_MAX_RUNTIME_SECONDS = 3600

_ACCEPTANCE_HEADING = re.compile(
    r"(?im)^\s*(?:#+\s*)?(?:\*\*)?\s*(?:приёмка|приемка|acceptance|критерии|"
    r"definition of done|done when)\b"
)
_ITEM = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+\S")


def _interpolate_minutes(table: "dict[int, int]", acceptance_items: int) -> int:
    """The table's figure for this item count: linear between points, clamped at both ends."""
    items = max(0, acceptance_items)
    points = sorted(table)
    if items <= points[0]:
        return table[points[0]]
    if items >= points[-1]:
        # Above the last measured point the figure is held, not extrapolated: the larger
        # buckets are fan-out parents whose wall clock falls back (7+ measures *lower*),
        # so a fitted slope would predict a bigger card as faster.
        return table[points[-1]]
    for lower, upper in zip(points, points[1:]):
        if lower <= items <= upper:
            span = upper - lower
            frac = (items - lower) / span
            return int(round(table[lower] + (table[upper] - table[lower]) * frac))
    return table[points[-1]]


def estimate_minutes(acceptance_items: int) -> int:
    """Planning minutes for a card with this many acceptance items: the measured p80.

    p80 and not the median: the wall axis exists to warn before a run is killed, so the
    figure that matters is the pessimistic-but-plausible one — 4 items ≈ 79 min, which
    is 2.2× the old linear guess and over the 60-minute default wall. ``3 items ≈ 81``
    is likewise over ``CARD_MINUTE_TARGET``; see ``estimate_minutes_median`` for the
    p50 that the advisory also reports.
    """
    return _interpolate_minutes(CALIBRATION_P80_MINUTES, acceptance_items)


def estimate_minutes_median(acceptance_items: int) -> int:
    """Median minutes for a card with this many acceptance items (the measured p50)."""
    return _interpolate_minutes(CALIBRATION_P50_MINUTES, acceptance_items)


def wall_minutes(max_runtime_seconds: Optional[int]) -> Optional[int]:
    """The wall expressed in whole minutes (rounded up), or None when the card has none."""
    if max_runtime_seconds is None:
        return None
    return int(math.ceil(max(0, max_runtime_seconds) / 60))


@dataclass(frozen=True)
class CardSizeEstimate:
    """Estimated iterations and minutes for a card, and whether they exceed a budget."""
    acceptance_items: int
    body_bytes: int
    estimated_iterations: int
    budget: Optional[int]
    # The card's own wall-clock cap (``tasks.max_runtime_seconds``); None = uncapped
    # (a row created before the default existed), which the rule reads as silence.
    max_runtime_seconds: Optional[int] = None

    @property
    def estimated_minutes(self) -> int:
        # Derived rather than stored: a hand-built estimate cannot disagree with the
        # calibration table the warning and the tool response both report.
        return estimate_minutes(self.acceptance_items)

    @property
    def median_minutes(self) -> int:
        return estimate_minutes_median(self.acceptance_items)

    @property
    def wall_minutes(self) -> Optional[int]:
        return wall_minutes(self.max_runtime_seconds)

    @property
    def fits(self) -> bool:
        """True when no budget was given (nothing to judge) or the estimate fits."""
        return self.budget is None or self.estimated_iterations <= self.budget

    @property
    def overrun(self) -> int:
        if self.budget is None:
            return 0
        return max(0, self.estimated_iterations - self.budget)

    @property
    def fits_minutes(self) -> bool:
        """True when no wall was given, the card names no criteria, or the p80 fits it."""
        if self.wall_minutes is None or not self.acceptance_items:
            return True
        return self.estimated_minutes <= self.wall_minutes

    @property
    def minutes_overrun(self) -> int:
        if self.wall_minutes is None or not self.acceptance_items:
            return 0
        return max(0, self.estimated_minutes - self.wall_minutes)


def count_acceptance_items(body: Optional[str]) -> int:
    """Bullet/numbered items under an acceptance heading; 0 when the card names none.

    A card that lists no criteria cannot be sized this way — returning 0 keeps the
    warning honest instead of inventing a number from prose length.
    """
    text = body or ""
    match = _ACCEPTANCE_HEADING.search(text)
    if not match:
        return 0
    section = text[match.end():]
    # Stop at the next heading: criteria of the NEXT section are not this card's work.
    nxt = re.search(r"(?m)^\s*#{1,6}\s+\S", section)
    if nxt:
        section = section[:nxt.start()]
    return sum(1 for line in section.splitlines() if _ITEM.match(line))


def estimate_iterations(acceptance_items: int) -> int:
    """Iterations the rule allots to a card with this many acceptance items."""
    return math.ceil(
        (BASE_ITERATIONS + ITERATIONS_PER_CRITERION * max(0, acceptance_items)) * SAFETY_MARGIN
    )


def estimate_card(
    body: Optional[str],
    budget: Optional[int],
    *,
    max_runtime_seconds: Optional[int] = None,
) -> CardSizeEstimate:
    """Size a card body against a profile's iteration budget and the card's wall.

    ``budget=None`` means unlimited (nothing to judge); ``max_runtime_seconds=None``
    means the card carries no wall (an uncapped row), *not* the dispatcher default —
    the caller that knows the default (``kanban_create``, because ``create_task`` is
    about to write it) resolves it before calling here.
    """
    items = count_acceptance_items(body)
    return CardSizeEstimate(
        acceptance_items=items,
        body_bytes=len((body or "").encode("utf-8")),
        estimated_iterations=estimate_iterations(items),
        budget=budget,
        max_runtime_seconds=max_runtime_seconds,
    )


def sizing_warning(estimate: CardSizeEstimate, *, assignee: str) -> Optional[str]:
    """A one-line advisory for ``kanban_create``; None when the card fits both axes.

    Two limits are reported because they are different ways to lose the run: the
    assignee's iteration budget and the card's own wall clock. Advisory, never fatal:
    the creator decides to split, to raise the profile's cap or to raise the wall, and
    all three are legitimate answers.
    """
    if not estimate.acceptance_items:
        # Nothing was declared, so there is nothing to size — including the minutes,
        # whose zero-item bucket is mostly cards that never named a criterion.
        return None
    iteration_over = estimate.budget is not None and not estimate.fits
    minute_over = not estimate.fits_minutes
    if not iteration_over and not minute_over:
        return None

    parts: list[str] = []
    if iteration_over:
        parts.append(
            f"{estimate.acceptance_items} acceptance items ≈ "
            f"{estimate.estimated_iterations} iterations vs {estimate.budget} for "
            f"'{assignee}'"
        )
    if minute_over:
        parts.append(
            f"{estimate.acceptance_items} acceptance items ≈ "
            f"{estimate.estimated_minutes} мин (медиана {estimate.median_minutes}) "
            f"vs предел прогона карточки {estimate.max_runtime_seconds} с "
            f"({estimate.wall_minutes} мин)"
        )
    if not parts:  # defensive: no axis over, nothing to say
        return None
    split = " or ".join(
        item for item, used in (
            ("raise `agent.max_turns` for that profile", iteration_over),
            ("raise `max_runtime_seconds` of the card", minute_over),
        ) if used
    )
    return (
        "card volume may exceed its budget: " + "; ".join(parts)
        + f". Цель — 30–{CARD_MINUTE_TARGET} мин на прогон. "
        f"Split the card, or {split}, or accept that it may stop mid-work "
        f"(the WIP checkpoint keeps the tree)."
    )


def assignee_iteration_budget(assignee: str) -> Optional[int]:
    """The iteration cap the assignee profile's worker will actually get, or None.

    ``kanban.dispatch`` spawns the worker under the assignee's ``HERMES_HOME``, so
    ``agent.max_turns`` of THAT profile's ``config.yaml`` is the cap — not the
    creator's. Unreadable/unset resolves to None (unlimited by the rule: no
    warning rather than a guessed budget).
    """
    try:
        from hermes_cli.config import read_user_config_raw, resolve_turn_limit
        from hermes_cli.profiles import get_profile_dir, normalize_profile_name

        name = normalize_profile_name(assignee)
        if name == "default":
            from hermes_constants import get_hermes_home
            profile_dir = get_hermes_home()
        else:
            profile_dir = get_profile_dir(name)
        config_path = profile_dir / "config.yaml"
        if not config_path.exists():
            return None
        raw = read_user_config_raw(config_path)
    except (ImportError, OSError, ValueError):
        # An unreadable profile config means "no budget known", which the rule
        # reports as silence rather than inventing a cap.
        return None
    agent_cfg = raw.get("agent") if isinstance(raw, dict) else None
    value = agent_cfg.get("max_turns") if isinstance(agent_cfg, dict) else None
    if value is None:
        value = raw.get("max_turns") if isinstance(raw, dict) else None
    from hermes_cli.config import TURN_LIMIT_UNLIMITED
    resolved = resolve_turn_limit(value)
    # An unlimited profile has no budget for a card to exceed.
    return None if resolved == TURN_LIMIT_UNLIMITED else resolved


def default_wall_seconds() -> Optional[int]:
    """The wall ``create_task`` writes into a card that names none; None if unreadable.

    Read from ``hermes_cli.kanban_db`` (one source of truth for the number) and never
    cached: the value is a board-level decision, not a constant of this rule.
    """
    try:
        from hermes_cli.kanban_db import DEFAULT_MAX_RUNTIME_SECONDS
    except (ImportError, AttributeError):
        return FALLBACK_MAX_RUNTIME_SECONDS
    return DEFAULT_MAX_RUNTIME_SECONDS


__all__ = [
    "BASE_ITERATIONS",
    "CALIBRATION_P50_MINUTES",
    "CALIBRATION_P80_MINUTES",
    "CARD_MINUTE_TARGET",
    "FALLBACK_MAX_RUNTIME_SECONDS",
    "ITERATIONS_PER_CRITERION",
    "CardSizeEstimate",
    "assignee_iteration_budget",
    "count_acceptance_items",
    "default_wall_seconds",
    "estimate_card",
    "estimate_iterations",
    "estimate_minutes",
    "estimate_minutes_median",
    "sizing_warning",
    "wall_minutes",
]
