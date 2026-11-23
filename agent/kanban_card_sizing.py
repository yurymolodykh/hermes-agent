"""Card-sizing rule: does a card's declared volume fit the assignee's iteration budget?

An iteration budget belongs to the *assignee profile* (``agent.max_turns``), not to
the card, and a card that cannot fit does not fail loudly — it burns its last
iteration mid-edit and stops (``t_f3d28e37``: 1459 verified lines, uncommitted).
Raising every profile's cap is not the fix: the same 31 budget stops in the board
history ran from 0.5 h to 2.8 h, so the cap is not the variable — the card's size
against it is.

The rule is deliberately coarse and stated in work items the card already names:
its acceptance criteria. Measured on 383 finished runs of the shared board (31 of
them budget stops, 352 not): the median budget stop carried 5 acceptance items and
the median completed run 4. Applied as written, the estimator flags 6 of the 31
stops (19 %) and 18 of the 352 other runs (5 %) — a 3.5x lift over the 8 % base
rate. That is the right asymmetry for an advisory warning: a false alarm costs a
re-read of the card, a missed one costs the run, and the WIP checkpoint (see
``agent.kanban_budget_checkpoint``) is the guarantee underneath for the stops this
rule does not see.

Budget stops are NOT mostly a size problem: 25 of 31 carried a normal-looking card.
The cap is not the variable and neither is the item count alone — which is why the
rule warns rather than blocks, and why the checkpoint, not the split, is what keeps
the work.

The rule is advisory by construction: it never blocks ``kanban_create``. A card
may legitimately be bigger than the profile and be split by its creator.
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

# Minutes are a second, independent axis: a card can fit its iteration budget and
# still run for two hours. Measured on the shared board (672 runs, 17–23.11.2026,
# successful runs only, duration = task_runs.ended_at − started_at): 65 % of runs
# landed under 50 min with 0–4 acceptance items, 43 % with 5–6. Text features (body
# length, file/line counts) separate duration poorly (AUC 0.58–0.60), so minutes are
# allotted from the one feature that does: the acceptance-item count.
BASE_MINUTES = 12
MINUTES_PER_ITEM = 6
# The owner's target for one run: a card is authored to close in 30–50 minutes.
CARD_MINUTE_TARGET = 50

_ACCEPTANCE_HEADING = re.compile(
    r"(?im)^\s*(?:#+\s*)?(?:\*\*)?\s*(?:приёмка|приемка|acceptance|критерии|"
    r"definition of done|done when)\b"
)
_ITEM = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+\S")


def estimate_minutes(acceptance_items: int) -> int:
    """Minutes the rule allots to a card with this many acceptance items.

    ``12 + 6 × items``: the fixed overhead of orienting and handing off plus six
    minutes per acceptance criterion. Fitted on the shared board's successful runs
    (see ``BASE_MINUTES``), so 3 items ≈ 30 min and 6 ≈ 48 min — the second lands
    just under ``CARD_MINUTE_TARGET`` and a 7th item pushes the card over it.
    """
    return BASE_MINUTES + MINUTES_PER_ITEM * max(0, acceptance_items)


@dataclass(frozen=True)
class CardSizeEstimate:
    """Estimated iterations and minutes for a card, and whether they exceed a budget."""
    acceptance_items: int
    body_bytes: int
    estimated_iterations: int
    budget: Optional[int]
    estimated_minutes: Optional[int] = None

    def __post_init__(self) -> None:
        # Derived rather than required: every caller that sizes a card gets the minute
        # figure for free, and a hand-built estimate cannot disagree with the formula.
        if self.estimated_minutes is None:
            object.__setattr__(
                self, "estimated_minutes", estimate_minutes(self.acceptance_items)
            )

    @property
    def fits(self) -> bool:
        """True when no budget was given (nothing to judge) or the estimate fits."""
        return self.budget is None or self.estimated_iterations <= self.budget

    @property
    def overrun(self) -> int:
        if self.budget is None:
            return 0
        return max(0, self.estimated_iterations - self.budget)


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


def estimate_card(body: Optional[str], budget: Optional[int]) -> CardSizeEstimate:
    """Size a card body against a profile's iteration budget (``None`` = unlimited)."""
    items = count_acceptance_items(body)
    return CardSizeEstimate(
        acceptance_items=items,
        body_bytes=len((body or "").encode("utf-8")),
        estimated_iterations=estimate_iterations(items),
        budget=budget,
    )


def sizing_warning(estimate: CardSizeEstimate, *, assignee: str) -> Optional[str]:
    """A one-line advisory for ``kanban_create``; None when the card fits.

    Advisory, never fatal: the creator decides to split or to raise the profile's
    cap, and both are legitimate answers.
    """
    if estimate.fits or estimate.budget is None:
        return None
    if not estimate.acceptance_items:
        return None
    return (
        f"card volume may exceed the assignee's iteration budget: "
        f"{estimate.acceptance_items} acceptance items ≈ "
        f"{estimate.estimated_iterations} iterations vs {estimate.budget} for "
        f"'{assignee}', ≈ {estimate.estimated_minutes} мин при цели 30–{CARD_MINUTE_TARGET}. "
        f"Split the card, or raise `agent.max_turns` for that profile, "
        f"or accept that it may stop mid-work (the WIP checkpoint keeps the tree)."
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


__all__ = [
    "BASE_ITERATIONS",
    "BASE_MINUTES",
    "CARD_MINUTE_TARGET",
    "ITERATIONS_PER_CRITERION",
    "MINUTES_PER_ITEM",
    "CardSizeEstimate",
    "assignee_iteration_budget",
    "count_acceptance_items",
    "estimate_card",
    "estimate_iterations",
    "estimate_minutes",
    "sizing_warning",
]
