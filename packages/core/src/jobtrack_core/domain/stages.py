"""The application lifecycle, as an explicit state machine.

This lives in the domain layer and nowhere else. The board enforces nothing — a
drag-and-drop that only offers legal targets is a convenience, not a control, because
the same transition is reachable through the API. Routers call :func:`transition` and
report what it says.

The machine is deliberately small. Stages move forward one step at a time, any stage
can end in one of three terminal states, and terminal states are terminal.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


class Stage(enum.StrEnum):
    """Where an application currently sits."""

    SAVED = "saved"
    APPLIED = "applied"
    SCREEN = "screen"
    TECHNICAL = "technical"
    ONSITE = "onsite"
    OFFER = "offer"

    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    GHOSTED = "ghosted"


#: The ordered pipeline. Position in this tuple is what "forward" means, and it is
#: also the order the board renders and the funnel reports.
PIPELINE: Final[tuple[Stage, ...]] = (
    Stage.SAVED,
    Stage.APPLIED,
    Stage.SCREEN,
    Stage.TECHNICAL,
    Stage.ONSITE,
    Stage.OFFER,
)

#: No transition leaves these.
TERMINAL: Final[frozenset[Stage]] = frozenset({Stage.REJECTED, Stage.WITHDRAWN, Stage.GHOSTED})


def _build_transitions() -> MappingProxyType[Stage, frozenset[Stage]]:
    """Derive the transition table from the pipeline rather than writing it out.

    A hand-written table of nine stages is fifty-odd entries that have to stay
    consistent with each other; deriving it means the rules are stated once.
    """
    table: dict[Stage, frozenset[Stage]] = {}
    for index, stage in enumerate(PIPELINE):
        allowed: set[Stage] = set(TERMINAL)
        # Forward exactly one step. Skipping stages would leave the funnel with
        # holes that analytics cannot distinguish from a stage nobody reached.
        if index + 1 < len(PIPELINE):
            allowed.add(PIPELINE[index + 1])
        # Back one step, because people correct mistakes. Reopening a terminal
        # application is not in this set: that is a new application.
        if index > 0:
            allowed.add(PIPELINE[index - 1])
        table[stage] = frozenset(allowed)

    for stage in TERMINAL:
        table[stage] = frozenset()

    return MappingProxyType(table)


#: stage -> the stages reachable from it in one step.
ALLOWED_TRANSITIONS: Final[MappingProxyType[Stage, frozenset[Stage]]] = _build_transitions()


class IllegalTransitionError(ValueError):
    """Raised when a requested stage change is not permitted.

    Carries both stages so a router can render a message without re-deriving them.
    """

    def __init__(self, current: Stage, requested: Stage) -> None:
        self.current = current
        self.requested = requested
        if current is requested:
            detail = f"already in {current.value}"
        elif current in TERMINAL:
            detail = f"{current.value} is terminal"
        else:
            allowed = ", ".join(sorted(s.value for s in ALLOWED_TRANSITIONS[current]))
            detail = f"from {current.value} only: {allowed}"
        super().__init__(f"cannot move to {requested.value}: {detail}")


@dataclass(frozen=True, slots=True)
class Transition:
    """A validated stage change, ready to be written as a `stage_events` row."""

    from_stage: Stage
    to_stage: Stage


def can_transition(current: Stage, requested: Stage) -> bool:
    """Whether ``requested`` is reachable from ``current`` in one step."""
    return requested in ALLOWED_TRANSITIONS[current]


def transition(current: Stage, requested: Stage) -> Transition:
    """Validate a stage change.

    Raises:
        IllegalTransitionError: if the move is not permitted.
    """
    if not can_transition(current, requested):
        raise IllegalTransitionError(current, requested)
    return Transition(from_stage=current, to_stage=requested)


def is_active(stage: Stage) -> bool:
    """Whether an application in this stage is still in play.

    Nudges only consider active applications, and the funnel counts them separately
    from applications that have ended.
    """
    return stage not in TERMINAL


def pipeline_index(stage: Stage) -> int | None:
    """Position in the pipeline, or ``None`` for a terminal stage."""
    try:
        return PIPELINE.index(stage)
    except ValueError:
        return None
