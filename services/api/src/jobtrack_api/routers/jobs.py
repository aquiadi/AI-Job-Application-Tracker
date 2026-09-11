"""Saving and reading job postings.

`POST /jobs` fetches synchronously and extracts asynchronously, and the split is
deliberate. Fetching a board's JSON takes a few hundred milliseconds and can fail in
ways the user can act on — an unsupported host, a deleted posting — so the answer
belongs in the response. Extraction takes seconds, depends on a model, and is
retryable, so it belongs behind the outbox.

The consequence is that a created job is `queued`, not `ready`, and the interface has
to show that. That is the honest shape: the work genuinely has not happened yet.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Self

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, update

from jobtrack_api.deps.auth import CurrentIdentity, TenantSession
from jobtrack_api.deps.runtime import AppRuntime
from jobtrack_api.errors import (
    NotFoundError,
    UnauthenticatedError,
    UnprocessableInputError,
    error_responses,
)
from jobtrack_core.db.enums import JobStatus, RequirementKind, SourceAts
from jobtrack_core.db.models import Application, Job, JobRequirement
from jobtrack_core.events import JOB, EventType
from jobtrack_core.ingest.adapters.base import IngestError
from jobtrack_core.ingest.router import fetch_posting, parse_pasted
from jobtrack_core.pipeline import jobs as pipeline
from jobtrack_core.scoring.fit import Coverage, FitScore, score_job
from jobtrack_core.storage import object_key

router = APIRouter(prefix="/jobs", tags=["jobs"])

#: A posting longer than this is a careers page, not a role.
MAX_PASTED_CHARS = 60_000


class SaveJob(BaseModel):
    """Either a link to a supported board, or the description text itself."""

    url: str | None = Field(default=None, max_length=2048)
    text: str | None = Field(default=None, max_length=MAX_PASTED_CHARS)

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if bool(self.url) == bool(self.text):
            raise ValueError("send either url or text, not both and not neither")
        return self


class RequirementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    text: str
    kind: RequirementKind
    #: False until the embedding worker has run. The interface shows a posting as
    #: still processing rather than showing requirements it cannot yet score.
    embedded: bool


class JobSummary(BaseModel):
    """One row of the saved list."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: JobStatus
    failure_reason: str | None
    source_ats: SourceAts
    source_url: str | None
    title: str | None
    company: str | None
    location: str | None
    created_at: datetime
    requirement_count: int
    #: Present once the user has started an application against this posting.
    application_id: uuid.UUID | None


class JobDetail(JobSummary):
    """A posting and everything extraction found in it."""

    seniority: str | None
    remote_policy: str | None
    employment_type: str | None
    experience_years_min: int | None
    experience_years_max: int | None
    salary_min: Decimal | None
    salary_max: Decimal | None
    salary_currency: str | None
    hard_skills: list[str]
    responsibilities: list[str]
    requirements: list[RequirementOut]


class MatchOut(BaseModel):
    """One requirement and the evidence that answered it."""

    requirement_id: uuid.UUID
    requirement: str
    kind: RequirementKind
    coverage: Coverage
    #: Cosine similarity of the chosen evidence. Shown because the judgement is a
    #: threshold on it, and hiding it would make the threshold unarguable.
    similarity: float
    evidence_item_id: uuid.UUID | None
    evidence: str | None


class ScoreOut(BaseModel):
    """The breakdown, and the number computed from it."""

    job_id: uuid.UUID
    score: int
    must_total: int
    must_covered: int
    nice_total: int
    nice_covered: int
    matches: list[MatchOut]
    #: False when the posting has no embedded requirements or the profile has no
    #: reviewed items. Distinct from a genuine zero, which means something else
    #: entirely to whoever is reading the page.
    scorable: bool
    #: The space the comparison ran in. Under the local backend this is `heuristic`,
    #: which is lexical rather than semantic, and the interface says so.
    embedding_model: str | None


class SavedJob(BaseModel):
    id: uuid.UUID
    status: JobStatus
    #: False when this posting was already saved. The interface says "already saved"
    #: rather than pretending a second one was created.
    created: bool


@router.get(
    "",
    summary="Postings the user has saved",
    responses=error_responses(UnauthenticatedError),
)
async def list_jobs(
    session: TenantSession,
    identity: CurrentIdentity,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[JobSummary]:
    """Saved postings, newest first."""
    counts = (
        select(JobRequirement.job_id, func.count().label("n"))
        .group_by(JobRequirement.job_id)
        .subquery()
    )
    statement = (
        select(Job, func.coalesce(counts.c.n, 0), Application.id)
        .outerjoin(counts, counts.c.job_id == Job.id)
        .outerjoin(Application, Application.job_id == Job.id)
        .order_by(Job.created_at.desc())
        .limit(limit)
    )
    if status_filter is not None:
        statement = statement.where(Job.status == status_filter)

    return [
        _summary(job, count, application_id)
        for job, count, application_id in (await session.execute(statement)).all()
    ]


@router.get(
    "/{job_id}",
    summary="One posting with its requirements",
    responses=error_responses(UnauthenticatedError, NotFoundError),
)
async def get_job(job_id: uuid.UUID, session: TenantSession) -> JobDetail:
    """A posting, its extracted fields, and every requirement found in it."""
    job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None:
        # Row-level security makes another user's posting indistinguishable from one
        # that does not exist, and this returns the same 404 for both on purpose.
        raise NotFoundError("job")

    requirements = list(
        (
            await session.execute(
                select(JobRequirement)
                .where(JobRequirement.job_id == job_id)
                .order_by(JobRequirement.display_order)
            )
        )
        .scalars()
        .all()
    )
    application_id = (
        await session.execute(select(Application.id).where(Application.job_id == job_id))
    ).scalar_one_or_none()

    return JobDetail(
        **_summary(job, len(requirements), application_id).model_dump(),
        seniority=job.seniority.value if job.seniority else None,
        remote_policy=job.remote_policy.value if job.remote_policy else None,
        employment_type=job.employment_type.value if job.employment_type else None,
        experience_years_min=job.experience_years_min,
        experience_years_max=job.experience_years_max,
        salary_min=job.salary_min,
        salary_max=job.salary_max,
        salary_currency=job.salary_currency,
        hard_skills=job.hard_skills,
        responsibilities=job.responsibilities,
        requirements=[
            RequirementOut(
                id=requirement.id,
                text=requirement.text,
                kind=requirement.kind,
                embedded=requirement.embedding is not None,
            )
            for requirement in requirements
        ],
    )


@router.post(
    "",
    summary="Save a posting",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(UnauthenticatedError, UnprocessableInputError),
)
async def save_job(
    body: SaveJob,
    session: TenantSession,
    identity: CurrentIdentity,
    runtime: AppRuntime,
    response: Response,
) -> SavedJob:
    """Fetch or accept a posting, store it, and queue it for extraction."""
    try:
        posting = (
            await fetch_posting(body.url)
            if body.url
            else parse_pasted(body.text or "", source_url="")
        )
    except IngestError as exc:
        # The message is the adapter's, which is written to be shown: it names the
        # boards that work, or says the posting is gone, or that the paste is too short.
        raise UnprocessableInputError(str(exc)) from exc

    result = await pipeline.ingest(session, user_id=identity.user_id, posting=posting)

    if not result.created:
        response.status_code = status.HTTP_200_OK
        return SavedJob(id=result.job_id, status=JobStatus.QUEUED, created=False)

    # Stored before the transaction commits, so a job row never exists pointing at an
    # object that was never written. The reverse — an object with no row — is harmless
    # and gets cleaned up by the bucket's lifecycle rule.
    uri = await runtime.raw.put(
        object_key(identity.user_id, "postings", f"{result.job_id}.json"),
        posting.model_dump_json().encode("utf-8"),
        content_type="application/json",
    )
    await session.execute(update(Job).where(Job.id == result.job_id).values(raw_gcs_uri=uri))

    return SavedJob(id=result.job_id, status=JobStatus.QUEUED, created=True)


@router.get(
    "/{job_id}/score",
    summary="How the profile matches this posting",
    responses=error_responses(UnauthenticatedError, NotFoundError),
)
async def get_score(job_id: uuid.UUID, session: TenantSession) -> ScoreOut:
    """Score this posting against the caller's reviewed profile items.

    Computed on read rather than stored. The inputs change whenever the user edits a
    profile item, and a cached score is a number that silently stops matching what it
    claims to describe. It is one query.
    """
    job = (await session.execute(select(Job.id).where(Job.id == job_id))).scalar_one_or_none()
    if job is None:
        raise NotFoundError("job")

    result: FitScore = await score_job(session, job_id=job_id)
    return ScoreOut(
        job_id=job_id,
        score=result.score,
        must_total=result.must_total,
        must_covered=result.must_covered,
        nice_total=result.nice_total,
        nice_covered=result.nice_covered,
        scorable=result.is_scorable,
        embedding_model=result.embedding_model,
        matches=[
            MatchOut(
                requirement_id=match.requirement_id,
                requirement=match.requirement,
                kind=match.kind,
                coverage=match.coverage,
                similarity=match.similarity,
                evidence_item_id=match.evidence_item_id,
                evidence=match.evidence,
            )
            for match in result.matches
        ],
    )


@router.post(
    "/{job_id}/retry",
    summary="Try a failed posting again",
    responses=error_responses(UnauthenticatedError, NotFoundError),
)
async def retry_job(
    job_id: uuid.UUID, session: TenantSession, identity: CurrentIdentity
) -> SavedJob:
    """Re-queue a posting whose extraction failed.

    Writes a fresh `job.ingested` event rather than calling extraction inline, so the
    retry takes exactly the path the first attempt took.
    """
    job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None:
        raise NotFoundError("job")

    job.status = JobStatus.QUEUED
    job.failure_reason = None
    await pipeline.emit(
        session,
        user_id=identity.user_id,
        event_type=EventType.JOB_INGESTED,
        aggregate_type=JOB,
        aggregate_id=job.id,
        payload={"content_hash": job.content_hash, "retry": True},
    )
    return SavedJob(id=job.id, status=JobStatus.QUEUED, created=False)


def _summary(job: Job, requirement_count: int, application_id: uuid.UUID | None) -> JobSummary:
    return JobSummary(
        id=job.id,
        status=job.status,
        failure_reason=job.failure_reason,
        source_ats=job.source_ats,
        source_url=job.source_url,
        title=job.title,
        company=job.company,
        location=job.location,
        created_at=job.created_at,
        requirement_count=requirement_count,
        application_id=application_id,
    )
