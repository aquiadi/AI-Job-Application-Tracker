"""The pipeline: postings the user is actually pursuing, and their stage history.

Two rules from CLAUDE.md are enforced here rather than trusted.

**Transitions are checked in the domain layer.** This router calls
`jobtrack_core.domain.stages.transition` and turns its refusal into a 409. The rule
about which moves are legal lives in one tested module, not in a router and again in
the interface.

**Analytics read events, never mutable state.** Every move appends a `stage_events`
row, and `applications.stage` is a cache of what those events already say. The funnel
is computed from the events, so a corrected stage does not rewrite history.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from jobtrack_api.deps.auth import CurrentIdentity, TenantSession
from jobtrack_api.errors import (
    ConflictError,
    NotFoundError,
    UnauthenticatedError,
    error_responses,
)
from jobtrack_core.db.models import Application, Job, StageEvent
from jobtrack_core.domain.stages import (
    PIPELINE,
    TERMINAL,
    IllegalTransitionError,
    Stage,
    is_active,
    transition,
)
from jobtrack_core.events import APPLICATION, EventType
from jobtrack_core.pipeline.jobs import emit

router = APIRouter(prefix="/applications", tags=["applications"])


class StartApplication(BaseModel):
    job_id: uuid.UUID
    note: str | None = Field(default=None, max_length=2000)


class MoveStage(BaseModel):
    to_stage: Stage
    note: str | None = Field(default=None, max_length=2000)
    #: Backdating exists because people update a tracker days after the interview.
    #: Time-in-stage is computed from these, so an honest date matters more than a
    #: tidy one.
    occurred_at: datetime | None = None


class StageEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_stage: Stage | None
    to_stage: Stage
    occurred_at: datetime
    note: str | None


class ApplicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    stage: Stage
    stage_entered_at: datetime
    notes: str | None
    created_at: datetime
    title: str | None
    company: str | None
    location: str | None
    source_url: str | None
    #: Whole days in the current stage. The nudge sweep reads the same figure, so the
    #: board shows exactly what will trigger a follow-up.
    days_in_stage: int
    is_active: bool
    allowed_next: list[Stage]


class ApplicationDetail(ApplicationOut):
    history: list[StageEventOut]


class BoardColumn(BaseModel):
    stage: Stage
    applications: list[ApplicationOut]


class Board(BaseModel):
    """The pipeline, grouped for display.

    Terminal stages are separate from the pipeline because they are outcomes rather
    than steps, and a board that shows Rejected as a column beside Onsite reads as
    somewhere applications are meant to arrive.
    """

    columns: list[BoardColumn]
    closed: list[ApplicationOut]


@router.get("", summary="The pipeline", responses=error_responses(UnauthenticatedError))
async def get_board(
    session: TenantSession,
    include_closed: Annotated[bool, Query()] = True,
) -> Board:
    """Every application, grouped by stage."""
    rows = await _load_all(session)
    by_stage: dict[Stage, list[ApplicationOut]] = {stage: [] for stage in PIPELINE}
    closed: list[ApplicationOut] = []

    for shaped in rows:
        if shaped.stage in TERMINAL:
            closed.append(shaped)
        else:
            by_stage[shaped.stage].append(shaped)

    return Board(
        columns=[BoardColumn(stage=stage, applications=by_stage[stage]) for stage in PIPELINE],
        closed=closed if include_closed else [],
    )


@router.post(
    "",
    summary="Start tracking a posting",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(UnauthenticatedError, NotFoundError, ConflictError),
)
async def start_application(
    body: StartApplication, session: TenantSession, identity: CurrentIdentity
) -> ApplicationDetail:
    """Create an application at `Saved`, with the event that put it there.

    The first `stage_events` row has a null `from_stage`. Without it the history would
    begin at the first move and the time an application spent in `Saved` — which is the
    stage most of them die in — would be unmeasurable.
    """
    job = (await session.execute(select(Job).where(Job.id == body.job_id))).scalar_one_or_none()
    if job is None:
        raise NotFoundError("job")

    existing = (
        await session.execute(select(Application.id).where(Application.job_id == body.job_id))
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("you are already tracking that posting")

    now = datetime.now(UTC)
    application = Application(
        user_id=identity.user_id,
        job_id=body.job_id,
        stage=Stage.SAVED,
        stage_entered_at=now,
        notes=body.note,
    )
    session.add(application)
    await session.flush()

    session.add(
        StageEvent(
            user_id=identity.user_id,
            application_id=application.id,
            from_stage=None,
            to_stage=Stage.SAVED,
            occurred_at=now,
            note=body.note,
        )
    )
    await emit(
        session,
        user_id=identity.user_id,
        event_type=EventType.APPLICATION_STAGE_CHANGED,
        aggregate_type=APPLICATION,
        aggregate_id=application.id,
        payload={"from": None, "to": Stage.SAVED.value},
    )
    await session.flush()
    return await _detail(session, application, job)


@router.get(
    "/{application_id}",
    summary="One application with its history",
    responses=error_responses(UnauthenticatedError, NotFoundError),
)
async def get_application(application_id: uuid.UUID, session: TenantSession) -> ApplicationDetail:
    application, job = await _load_one(session, application_id)
    return await _detail(session, application, job)


@router.post(
    "/{application_id}/stage",
    summary="Move an application",
    responses=error_responses(UnauthenticatedError, NotFoundError, ConflictError),
)
async def move_stage(
    application_id: uuid.UUID,
    body: MoveStage,
    session: TenantSession,
    identity: CurrentIdentity,
) -> ApplicationDetail:
    """Move to a new stage, appending the event that records it."""
    application, job = await _load_one(session, application_id)

    try:
        move = transition(application.stage, body.to_stage)
    except IllegalTransitionError as exc:
        # The domain layer owns which moves are legal. This turns its refusal into a
        # status code and nothing else; duplicating the rule here is how the two
        # would drift.
        raise ConflictError(str(exc)) from exc

    occurred_at = body.occurred_at or datetime.now(UTC)
    application.stage = move.to_stage
    application.stage_entered_at = occurred_at

    session.add(
        StageEvent(
            user_id=identity.user_id,
            application_id=application.id,
            from_stage=move.from_stage,
            to_stage=move.to_stage,
            occurred_at=occurred_at,
            note=body.note,
        )
    )
    await emit(
        session,
        user_id=identity.user_id,
        event_type=EventType.APPLICATION_STAGE_CHANGED,
        aggregate_type=APPLICATION,
        aggregate_id=application.id,
        payload={"from": move.from_stage.value, "to": move.to_stage.value},
    )
    await session.flush()
    return await _detail(session, application, job)


@router.delete(
    "/{application_id}",
    summary="Stop tracking",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(UnauthenticatedError, NotFoundError),
)
async def delete_application(application_id: uuid.UUID, session: TenantSession) -> None:
    """Remove an application and its history.

    Distinct from withdrawing. Withdrawn is an outcome worth keeping in the funnel;
    deletion is for something tracked by mistake.
    """
    application, _ = await _load_one(session, application_id)
    await session.delete(application)


async def _load_all(session: TenantSession) -> list[ApplicationOut]:
    rows = (
        await session.execute(
            select(Application, Job)
            .join(Job, Job.id == Application.job_id)
            .order_by(Application.stage_entered_at.desc())
        )
    ).all()
    return [_shape(application, job) for application, job in rows]


async def _load_one(session: TenantSession, application_id: uuid.UUID) -> tuple[Application, Job]:
    row = (
        await session.execute(
            select(Application, Job)
            .join(Job, Job.id == Application.job_id)
            .where(Application.id == application_id)
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError("application")
    application, job = row
    return application, job


async def _detail(session: TenantSession, application: Application, job: Job) -> ApplicationDetail:
    history = list(
        (
            await session.execute(
                select(StageEvent)
                .where(StageEvent.application_id == application.id)
                .order_by(StageEvent.occurred_at, StageEvent.created_at)
            )
        )
        .scalars()
        .all()
    )
    return ApplicationDetail(
        **_shape(application, job).model_dump(),
        history=[StageEventOut.model_validate(event) for event in history],
    )


def _shape(application: Application, job: Job) -> ApplicationOut:
    return ApplicationOut(
        id=application.id,
        job_id=application.job_id,
        stage=application.stage,
        stage_entered_at=application.stage_entered_at,
        notes=application.notes,
        created_at=application.created_at,
        title=job.title,
        company=job.company,
        location=job.location,
        source_url=job.source_url,
        days_in_stage=(datetime.now(UTC) - application.stage_entered_at).days,
        is_active=is_active(application.stage),
        allowed_next=_allowed(application.stage),
    )


def _allowed(stage: Stage) -> list[Stage]:
    """Stages this application can move to, so the interface offers only legal moves.

    Derived from the same transition function the write path uses. A hardcoded list
    here would be a second copy of the rule, and the two would part company the first
    time a stage was added.
    """
    allowed = []
    for candidate in Stage:
        if candidate is stage:
            continue
        try:
            transition(stage, candidate)
        except IllegalTransitionError:
            continue
        allowed.append(candidate)
    return allowed
