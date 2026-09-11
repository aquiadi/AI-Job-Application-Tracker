"""Ingesting a posting, extracting it, and embedding what came out.

Three steps, each one idempotent, because every one of them can be retried. The API
writes the job; a handler extracts it; a handler embeds it. They are separate because
extraction is slow enough that doing it inside a request would make the request slow,
and because a failed extraction has to be retryable without re-creating the job.

Idempotency is not incidental here. At-least-once delivery means every handler will
eventually run twice on the same input, and the correct behaviour on the second run is
to notice the work is done and stop, not to do it again and write a duplicate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobtrack_core.db.enums import JobStatus, LlmOperation, LlmOutcome
from jobtrack_core.db.models import JdExtractionCache, Job, JobRequirement, LlmCall, Outbox
from jobtrack_core.events import JOB, Event, EventType
from jobtrack_core.ingest.canonical import CANONICAL_SCHEMA_VERSION, CanonicalPosting
from jobtrack_core.ingest.extraction import ExtractedPosting
from jobtrack_core.llm.client import (
    EmbeddingTaskType,
    InvalidOutputError,
    LlmClient,
    LlmError,
    LlmResult,
)
from jobtrack_core.llm.prompts import load_prompt
from jobtrack_core.logs import get_logger

log = get_logger(__name__)

#: How many requirement texts go to the embedding model in one call.
EMBED_CHUNK = 100


@dataclass(frozen=True, slots=True)
class Ingested:
    """What `ingest` did, so the caller can tell the user which of the two happened."""

    job_id: uuid.UUID
    created: bool


async def ingest(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    posting: CanonicalPosting,
) -> Ingested:
    """Record a posting and announce it. Fast enough to run inside a request.

    Saving the same posting twice returns the first one rather than creating a second.
    That is the unique constraint on `(user_id, content_hash)` doing the work, not a
    prior read: two requests racing each other would both pass a read-then-write.
    """
    digest = posting.content_hash
    statement = (
        insert(Job)
        .values(
            user_id=user_id,
            status=JobStatus.QUEUED,
            source_ats=posting.source_ats,
            source_url=posting.source_url,
            content_hash=digest,
            schema_version=CANONICAL_SCHEMA_VERSION,
            title=posting.title,
            company=posting.company,
            location=posting.location,
        )
        .on_conflict_do_nothing(index_elements=[Job.user_id, Job.content_hash])
        .returning(Job.id)
    )
    created_id = (await session.execute(statement)).scalar_one_or_none()

    if created_id is None:
        existing = (
            await session.execute(
                select(Job.id).where(Job.user_id == user_id, Job.content_hash == digest)
            )
        ).scalar_one()
        return Ingested(job_id=existing, created=False)

    await emit(
        session,
        user_id=user_id,
        event_type=EventType.JOB_INGESTED,
        aggregate_type=JOB,
        aggregate_id=created_id,
        payload={"content_hash": digest, "source_ats": posting.source_ats.value},
    )
    return Ingested(job_id=created_id, created=True)


async def extract(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    posting: CanonicalPosting,
    client: LlmClient,
) -> Job:
    """Turn a posting into requirements, using the shared cache when it can.

    The cache is global and holds only posting content, so two users who saved the same
    Greenhouse listing pay for one extraction between them. It is keyed by content hash
    *and* schema version, so bumping the canonical schema re-extracts rather than
    serving a stale shape.
    """
    job = await _load(session, user_id=user_id, job_id=job_id)
    if job.status is JobStatus.READY:
        # Already done. At-least-once delivery guarantees this path is taken.
        return job

    job.status = JobStatus.EXTRACTING
    await session.flush()

    cached = await session.get(JdExtractionCache, (job.content_hash, CANONICAL_SCHEMA_VERSION))
    if cached is not None:
        extracted = ExtractedPosting.model_validate(cached.extracted)
        model = cached.model
    else:
        try:
            result = await _call_model(session, user_id=user_id, posting=posting, client=client)
        except (InvalidOutputError, LlmError) as exc:
            job.status = JobStatus.FAILED
            job.failure_reason = _reason(exc)
            await emit(
                session,
                user_id=user_id,
                event_type=EventType.JOB_FAILED,
                aggregate_type=JOB,
                aggregate_id=job.id,
                payload={"stage": "extract"},
            )
            return job

        extracted = result.value
        model = result.model
        # Written from the extraction result, never from the job row: a `Job` carries
        # a user_id and this table must never hold anything user-derived.
        await session.execute(
            insert(JdExtractionCache)
            .values(
                content_hash=job.content_hash,
                schema_version=CANONICAL_SCHEMA_VERSION,
                extracted=extracted.model_dump(mode="json"),
                model=model,
            )
            .on_conflict_do_nothing(
                index_elements=[JdExtractionCache.content_hash, JdExtractionCache.schema_version]
            )
        )

    _apply(job, extracted, posting=posting)
    await _replace_requirements(session, job=job, extracted=extracted)

    job.status = JobStatus.EMBEDDING
    await emit(
        session,
        user_id=user_id,
        event_type=EventType.JOB_EXTRACTED,
        aggregate_type=JOB,
        aggregate_id=job.id,
        payload={"model": model, "requirements": len(extracted.requirements)},
    )
    return job


async def embed(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    client: LlmClient,
) -> Job:
    """Embed the requirements that do not have a vector yet."""
    job = await _load(session, user_id=user_id, job_id=job_id)
    if job.status is JobStatus.FAILED:
        return job

    pending = list(
        (
            await session.execute(
                select(JobRequirement)
                .where(JobRequirement.job_id == job_id, JobRequirement.embedding.is_(None))
                .order_by(JobRequirement.display_order)
            )
        )
        .scalars()
        .all()
    )

    for offset in range(0, len(pending), EMBED_CHUNK):
        chunk = pending[offset : offset + EMBED_CHUNK]
        try:
            result = await client.embed(
                [requirement.text for requirement in chunk],
                task_type=EmbeddingTaskType.SEMANTIC_SIMILARITY,
            )
        except LlmError as exc:
            job.status = JobStatus.FAILED
            job.failure_reason = _reason(exc)
            await emit(
                session,
                user_id=user_id,
                event_type=EventType.JOB_FAILED,
                aggregate_type=JOB,
                aggregate_id=job.id,
                payload={"stage": "embed"},
            )
            return job

        embedded_at = datetime.now(UTC)
        for requirement, vector in zip(chunk, result.vectors, strict=True):
            requirement.embedding = vector
            requirement.embedding_model = result.model
            requirement.embedding_dim = result.dim
            requirement.embedding_task_type = result.task_type.value
            requirement.embedded_at = embedded_at

        await _record_call(
            session,
            user_id=user_id,
            model=result.model,
            prompt_id="embed",
            prompt_version="v1",
            operation=LlmOperation.EMBED,
            usage=result.usage,
            latency_ms=result.latency_ms,
            cost=result.estimated_cost_usd,
        )

    job.status = JobStatus.READY
    job.failure_reason = None
    await emit(
        session,
        user_id=user_id,
        event_type=EventType.JOB_READY,
        aggregate_type=JOB,
        aggregate_id=job.id,
        payload={"requirements": len(pending)},
    )
    return job


async def emit(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    event_type: EventType,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    payload: dict[str, Any],
) -> uuid.UUID:
    """Write one outbox row inside the caller's transaction."""
    row = Outbox(
        user_id=user_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type.value,
        payload=payload,
    )
    session.add(row)
    await session.flush()
    return row.id


def to_event(row: Outbox) -> Event:
    """The published form of an outbox row."""
    return Event(
        event_id=row.id,
        event_type=EventType(row.event_type),
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        user_id=row.user_id,
        payload=row.payload,
        occurred_at=row.created_at,
    )


async def _load(session: AsyncSession, *, user_id: uuid.UUID, job_id: uuid.UUID) -> Job:
    job = (
        await session.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    ).scalar_one_or_none()
    if job is None:
        # Under row-level security a job belonging to someone else reads as absent,
        # which is the same answer as a job that never existed. That is deliberate:
        # distinguishing them would confirm the row exists.
        raise LookupError(f"job {job_id} not found")
    return job


async def _call_model(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    posting: CanonicalPosting,
    client: LlmClient,
) -> LlmResult[ExtractedPosting]:
    prompt = load_prompt("extract_jd").render(
        posting=posting.body,
        title=posting.title or "not stated",
        company=posting.company or "not stated",
    )
    result = await client.generate_structured(prompt, ExtractedPosting)
    await _record_call(
        session,
        user_id=user_id,
        model=result.model,
        prompt_id=result.prompt_id,
        prompt_version=result.prompt_version,
        operation=LlmOperation.EXTRACT_JD,
        usage=result.usage,
        latency_ms=result.latency_ms,
        cost=result.estimated_cost_usd,
        attempts=result.attempts,
        validation_errors=result.validation_errors,
    )
    return result


def _apply(job: Job, extracted: ExtractedPosting, *, posting: CanonicalPosting) -> None:
    """Copy extracted fields onto the job.

    The adapter's title and company win where it has them. Greenhouse states both as
    structured fields, and a structured field from the board beats the same fact read
    back out of prose.
    """
    job.title = posting.title or extracted.title
    job.company = posting.company or extracted.company
    job.location = posting.location or extracted.location
    job.remote_policy = extracted.remote_policy
    job.employment_type = extracted.employment_type
    job.seniority = extracted.seniority
    job.experience_years_min = extracted.experience_years_min
    job.experience_years_max = extracted.experience_years_max
    job.salary_min = extracted.salary_min
    job.salary_max = extracted.salary_max
    job.salary_currency = extracted.salary_currency
    job.salary_period = extracted.salary_period
    job.hard_skills = list(extracted.hard_skills)
    job.soft_skills = list(extracted.soft_skills)
    job.responsibilities = list(extracted.responsibilities)


async def _replace_requirements(
    session: AsyncSession, *, job: Job, extracted: ExtractedPosting
) -> None:
    """Delete and reinsert, so a re-extraction does not leave the previous run behind.

    Requirements have no stable identity across extractions — the model may split one
    into two — so there is nothing to update in place against.
    """
    await session.execute(delete(JobRequirement).where(JobRequirement.job_id == job.id))
    for order, requirement in enumerate(extracted.ordered_requirements()):
        session.add(
            JobRequirement(
                user_id=job.user_id,
                job_id=job.id,
                text=requirement.text,
                kind=requirement.kind,
                display_order=order,
            )
        )
    await session.flush()


async def _record_call(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    model: str,
    prompt_id: str,
    prompt_version: str,
    operation: LlmOperation,
    usage: Any,
    latency_ms: int,
    cost: Any,
    attempts: int = 1,
    validation_errors: list[dict[str, Any]] | None = None,
) -> None:
    session.add(
        LlmCall(
            user_id=user_id,
            model=model,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            operation=operation,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            latency_ms=latency_ms,
            estimated_cost_usd=cost,
            outcome=LlmOutcome.OK if attempts == 1 else LlmOutcome.INVALID_OUTPUT,
            validation_errors={"errors": validation_errors} if validation_errors else None,
        )
    )


def _reason(exc: Exception) -> str:
    """A failure message safe to store and show.

    The exception type and a fixed message only. An exception from the model layer can
    carry posting text in its string form, and `jobs.failure_reason` is rendered in the
    interface.
    """
    if isinstance(exc, InvalidOutputError):
        fields = sorted(
            {".".join(str(part) for part in error.get("loc", [])) for error in exc.errors}
        )
        return f"the model's answer did not match the schema ({', '.join(fields) or 'unknown'})"
    return "extraction could not be completed"
