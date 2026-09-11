"""Tailored documents, and the follow-up drafts that go with them.

Two things in here are promises the product makes, and both are enforced by what the
code does not contain.

**A generated bullet that is not grounded never reaches a document.** The validator runs
before the artifact is written, not as a review step afterwards, so there is no path
that stores an unvalidated bullet. ADR 13.

**A nudge is never sent.** There is no send endpoint, no mail client, and no
credentials for one. A draft is text the user copies.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from jobtrack_api.deps.auth import CurrentIdentity, TenantSession
from jobtrack_api.deps.runtime import AppRuntime
from jobtrack_api.errors import (
    ConflictError,
    NotFoundError,
    UnauthenticatedError,
    UnprocessableInputError,
    error_responses,
)
from jobtrack_core.db.enums import ArtifactKind, NudgeOutcome, NudgeState
from jobtrack_core.db.models import Application, Artifact, Job, Nudge
from jobtrack_core.domain.stages import Stage
from jobtrack_core.llm.client import LlmError
from jobtrack_core.pipeline import nudges as nudge_pipeline
from jobtrack_core.pipeline import tailor as tailor_pipeline

router = APIRouter(tags=["documents"])


class ArtifactSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    application_id: uuid.UUID
    kind: ArtifactKind
    version: int
    created_at: datetime
    bullet_count: int
    #: Bullets the grounding validator refused, with the reason. Shown rather than
    #: swallowed: a silently shorter resume is worse than a visible gap.
    warnings: list[str]


class TailorOut(BaseModel):
    artifact: ArtifactSummary
    #: The backend that wrote it. `heuristic` selects and orders the user's own lines
    #: without rewriting them, which is a different document from a model's.
    model: str


class NudgeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    application_id: uuid.UUID
    stage: Stage
    state: NudgeState
    draft_subject: str | None
    draft_body: str | None
    created_at: datetime
    title: str | None
    company: str | None


class NudgeUpdate(BaseModel):
    state: NudgeState | None = None
    draft_subject: str | None = Field(default=None, max_length=200)
    draft_body: str | None = Field(default=None, max_length=2000)
    outcome: NudgeOutcome | None = None
    snoozed_until: datetime | None = None


class SweepOut(BaseModel):
    considered: int
    drafted: int
    already_had_one: int


@router.post(
    "/applications/{application_id}/tailor",
    summary="Generate a tailored resume",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(UnauthenticatedError, NotFoundError, UnprocessableInputError),
)
async def tailor(
    application_id: uuid.UUID,
    session: TenantSession,
    identity: CurrentIdentity,
    runtime: AppRuntime,
) -> TailorOut:
    """Write a resume for this application from reviewed profile items only.

    Every bullet is checked against the items it cites before the artifact is stored.
    Anything that fails is dropped and the reason is recorded, so the response can be
    honest about what is not in the document.
    """
    application = (
        await session.execute(select(Application).where(Application.id == application_id))
    ).scalar_one_or_none()
    if application is None:
        raise NotFoundError("application")

    try:
        result = await tailor_pipeline.tailor_resume(
            session,
            user_id=identity.user_id,
            application_id=application_id,
            job_id=application.job_id,
            client=runtime.llm,
        )
    except tailor_pipeline.NoEvidenceError as exc:
        raise UnprocessableInputError(str(exc)) from exc
    except LlmError as exc:
        raise UnprocessableInputError("the document could not be generated. Try again.") from exc

    artifact = (
        await session.execute(select(Artifact).where(Artifact.id == result.artifact_id))
    ).scalar_one()
    return TailorOut(artifact=_artifact(artifact), model=result.model)


@router.get(
    "/applications/{application_id}/artifacts",
    summary="Documents generated for an application",
    responses=error_responses(UnauthenticatedError),
)
async def list_artifacts(
    application_id: uuid.UUID, session: TenantSession
) -> list[ArtifactSummary]:
    """Every version, newest first. Versions are kept so an earlier draft is recoverable."""
    rows = (
        (
            await session.execute(
                select(Artifact)
                .where(Artifact.application_id == application_id)
                .order_by(Artifact.version.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_artifact(artifact) for artifact in rows]


@router.get(
    "/artifacts/{artifact_id}/pdf",
    summary="Download a document",
    response_class=Response,
    responses=error_responses(UnauthenticatedError, NotFoundError),
)
async def download(artifact_id: uuid.UUID, session: TenantSession) -> Response:
    """Render the stored structure to PDF.

    Rendered on demand rather than at generation time, so contact details can be
    corrected without paying for the document again. This is the only place in the
    system where those details are attached to generated text.
    """
    try:
        pdf = await tailor_pipeline.render(session, artifact_id=artifact_id)
    except LookupError as exc:
        raise NotFoundError("artifact") from exc

    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="resume-{artifact_id}.pdf"'},
    )


@router.get(
    "/nudges",
    summary="Drafted follow-ups",
    responses=error_responses(UnauthenticatedError),
)
async def list_nudges(session: TenantSession, include_dismissed: bool = False) -> list[NudgeOut]:
    """Follow-ups drafted for applications that have gone quiet. None has been sent."""
    statement = (
        select(Nudge, Job)
        .join(Application, Application.id == Nudge.application_id)
        .join(Job, Job.id == Application.job_id)
        .order_by(Nudge.created_at.desc())
    )
    if not include_dismissed:
        statement = statement.where(Nudge.state != NudgeState.DISMISSED)

    return [
        NudgeOut(
            id=nudge.id,
            application_id=nudge.application_id,
            stage=nudge.stage,
            state=nudge.state,
            draft_subject=nudge.draft_subject,
            draft_body=nudge.draft_body,
            created_at=nudge.created_at,
            title=job.title,
            company=job.company,
        )
        for nudge, job in (await session.execute(statement)).all()
    ]


@router.patch(
    "/nudges/{nudge_id}",
    summary="Edit or dismiss a draft",
    responses=error_responses(UnauthenticatedError, NotFoundError),
)
async def update_nudge(nudge_id: uuid.UUID, body: NudgeUpdate, session: TenantSession) -> NudgeOut:
    """Edit the text, mark it sent, or dismiss it.

    Marking it sent records an outcome; it does not send anything. The user sends from
    their own mail client, which is what makes "nothing is sent on your behalf" a
    property of the system rather than a promise in a document.
    """
    row = (
        await session.execute(
            select(Nudge, Job)
            .join(Application, Application.id == Nudge.application_id)
            .join(Job, Job.id == Application.job_id)
            .where(Nudge.id == nudge_id)
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError("nudge")

    nudge, job = row
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(nudge, field, value)
    # Editing the text is itself a decision about the draft, so it is recorded as one
    # rather than leaving the state saying nobody has looked at it.
    if body.draft_body is not None and body.state is None:
        nudge.state = NudgeState.EDITED
    await session.flush()

    return NudgeOut(
        id=nudge.id,
        application_id=nudge.application_id,
        stage=nudge.stage,
        state=nudge.state,
        draft_subject=nudge.draft_subject,
        draft_body=nudge.draft_body,
        created_at=nudge.created_at,
        title=job.title,
        company=job.company,
    )


@router.post(
    "/nudges/sweep",
    summary="Look for applications that have gone quiet",
    responses=error_responses(UnauthenticatedError, ConflictError),
)
async def sweep(session: TenantSession, identity: CurrentIdentity, runtime: AppRuntime) -> SweepOut:
    """Draft a follow-up for every application past its stage threshold.

    Safe to call repeatedly: the unique constraint on
    `(application_id, stage, stage_entered_at)` means one wait produces one draft, no
    matter how many times this runs. In cloud, Cloud Scheduler calls it daily.
    """
    result = await nudge_pipeline.sweep(session, user_id=identity.user_id, client=runtime.llm)
    return SweepOut(
        considered=result.considered,
        drafted=result.drafted,
        already_had_one=result.already_had_one,
    )


def _artifact(artifact: Artifact) -> ArtifactSummary:
    sections = artifact.content.get("sections", []) if artifact.content else []
    bullets = sum(len(section.get("bullets", [])) for section in sections)
    return ArtifactSummary(
        id=artifact.id,
        application_id=artifact.application_id,
        kind=artifact.kind,
        version=artifact.version,
        created_at=artifact.created_at,
        bullet_count=bullets,
        warnings=list(artifact.warnings),
    )
