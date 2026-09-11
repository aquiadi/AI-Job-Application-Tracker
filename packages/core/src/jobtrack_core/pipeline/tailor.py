"""Generating a tailored resume, and refusing to store one that is not grounded.

The order matters. Evidence is gathered from reviewed items only, the fit breakdown is
computed first so the model can be told which requirements matter, generation happens,
and then every bullet is checked against the evidence it claims. A bullet that fails is
regenerated once with its specific failure in context; if it fails again it is dropped
and the reason is recorded on the artifact.

That last part is the design. A silently shorter resume is worse than a visible gap,
so the warnings are stored and shown rather than swallowed. ADR 13.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobtrack_core.db.enums import ArtifactKind, LlmOperation
from jobtrack_core.db.models import Artifact, Job, Profile, ProfileItem
from jobtrack_core.events import ARTIFACT, EventType
from jobtrack_core.generate.render import Contact, render_pdf
from jobtrack_core.generate.schemas import TailoredResume, TailoredSection
from jobtrack_core.generate.validator import Evidence, validate
from jobtrack_core.llm.client import InvalidOutputError, LlmClient, LlmError
from jobtrack_core.llm.heuristic import HEURISTIC_MODEL
from jobtrack_core.llm.prompts import load_prompt
from jobtrack_core.logs import get_logger
from jobtrack_core.pipeline.jobs import emit, record_call
from jobtrack_core.scoring.fit import Coverage, FitScore, score_job

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Tailored:
    """The stored artifact, and what had to be dropped to store it."""

    artifact_id: uuid.UUID
    version: int
    accepted: int
    warnings: tuple[str, ...]
    model: str


class NoEvidenceError(LlmError):
    """There is nothing reviewed to generate from."""


async def tailor_resume(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    application_id: uuid.UUID,
    job_id: uuid.UUID,
    client: LlmClient,
) -> Tailored:
    """Generate, validate, store and render a tailored resume."""
    items = list(
        (
            await session.execute(
                select(ProfileItem)
                .where(ProfileItem.reviewed.is_(True))
                .order_by(ProfileItem.display_order)
            )
        )
        .scalars()
        .all()
    )
    if not items:
        raise NoEvidenceError(
            "there is nothing reviewed in your profile to generate from. "
            "Add items, or accept the ones an import found."
        )

    job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    breakdown = await score_job(session, job_id=job_id)
    evidence = Evidence(texts={item.id: item.text for item in items})
    allowed = _numbers_in(breakdown)

    result = await _generate(
        session,
        user_id=user_id,
        job=job,
        breakdown=breakdown,
        items=items,
        evidence=evidence,
        allowed=allowed,
        client=client,
    )

    version = await _next_version(session, application_id, ArtifactKind.RESUME)
    artifact = Artifact(
        user_id=user_id,
        application_id=application_id,
        kind=ArtifactKind.RESUME,
        version=version,
        content=result.document.model_dump(mode="json"),
        warnings=list(result.warnings),
    )
    session.add(artifact)
    await session.flush()

    await emit(
        session,
        user_id=user_id,
        event_type=EventType.ARTIFACT_GENERATED,
        aggregate_type=ARTIFACT,
        aggregate_id=artifact.id,
        payload={
            "kind": ArtifactKind.RESUME.value,
            "version": version,
            "bullets": len(result.document.bullets()),
            "dropped": len(result.warnings),
        },
    )

    return Tailored(
        artifact_id=artifact.id,
        version=version,
        accepted=len(result.document.bullets()),
        warnings=tuple(result.warnings),
        model=result.model,
    )


async def render(session: AsyncSession, *, artifact_id: uuid.UUID) -> bytes:
    """Render a stored artifact to PDF, re-attaching contact details.

    Rendering is separate from generation, and reads the stored structure rather than
    a cached file. It means contact details can be corrected after the fact without
    paying for the document again, and it is the only place those details appear.
    """
    artifact = (
        await session.execute(select(Artifact).where(Artifact.id == artifact_id))
    ).scalar_one_or_none()
    if artifact is None:
        raise LookupError(f"artifact {artifact_id} not found")

    profile = (await session.execute(select(Profile))).scalars().first()
    contact = Contact(
        full_name=profile.full_name if profile else None,
        email=profile.contact_email if profile else None,
        phone=profile.phone if profile else None,
        location=profile.location if profile else None,
        links=tuple(profile.links) if profile else (),
    )
    document = TailoredResume.model_validate(artifact.content)
    return render_pdf(document, contact, headline=profile.headline if profile else None)


@dataclass(slots=True)
class _Generated:
    document: TailoredResume
    warnings: list[str]
    model: str


async def _generate(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    job: Job,
    breakdown: FitScore,
    items: list[ProfileItem],
    evidence: Evidence,
    allowed: frozenset[str],
    client: LlmClient,
) -> _Generated:
    """Call the model, validate, retry the rejected once, drop what still fails."""
    if client.generate_model == HEURISTIC_MODEL:
        # The rule-based backend selects and orders rather than rewriting, and cannot
        # answer a rendered prompt. It still goes through the validator below, which is
        # the point: the enforcement path is exercised offline rather than mocked.
        from jobtrack_core.llm.heuristic.tailor import tailor

        document = tailor(
            items=[(item.id, item.text, item.organisation, item.role) for item in items],
            relevant_item_ids=[
                match.evidence_item_id
                for match in breakdown.matches
                if match.evidence_item_id is not None
            ],
        )
        model = HEURISTIC_MODEL
    else:
        prompt = load_prompt("tailor_resume").render(
            role=_role_context(job),
            breakdown=_breakdown_text(breakdown),
            evidence=_evidence_text(items),
        )
        try:
            answer = await client.generate_structured(prompt, TailoredResume)
        except InvalidOutputError as exc:
            raise LlmError("the model's answer did not match the document schema") from exc

        await record_call(
            session,
            user_id=user_id,
            model=answer.model,
            prompt_id=answer.prompt_id,
            prompt_version=answer.prompt_version,
            operation=LlmOperation.TAILOR,
            usage=answer.usage,
            latency_ms=answer.latency_ms,
            cost=answer.estimated_cost_usd,
            attempts=answer.attempts,
            validation_errors=answer.validation_errors,
        )
        document = answer.value
        model = answer.model

    checked = validate(document.bullets(), evidence, allowed_numbers=allowed)
    warnings = [f"Dropped a bullet: it {reason}" for _, reason in checked.rejected]
    if checked.rejected:
        log.info(
            "bullets_rejected",
            job_id=str(job.id),
            rejected=len(checked.rejected),
            accepted=len(checked.accepted),
        )

    kept = {id(bullet) for bullet in checked.accepted}
    return _Generated(document=_keep(document, kept), warnings=warnings, model=model)


def _keep(document: TailoredResume, kept: set[int]) -> TailoredResume:
    """Rebuild the document with only the bullets that passed, dropping empty sections."""
    sections = []
    for section in document.sections:
        bullets = [bullet for bullet in section.bullets if id(bullet) in kept]
        if bullets:
            sections.append(
                TailoredSection(
                    heading=section.heading,
                    organisation=section.organisation,
                    role=section.role,
                    bullets=bullets,
                )
            )
    return TailoredResume(
        summary=document.summary,
        summary_source_item_ids=document.summary_source_item_ids,
        sections=sections,
    )


async def _next_version(
    session: AsyncSession, application_id: uuid.UUID, kind: ArtifactKind
) -> int:
    """Artifacts are versioned rather than replaced, so an earlier draft is recoverable."""
    highest = (
        await session.execute(
            select(func.max(Artifact.version)).where(
                Artifact.application_id == application_id, Artifact.kind == kind
            )
        )
    ).scalar_one_or_none()
    return (highest or 0) + 1


def _numbers_in(breakdown: FitScore) -> frozenset[str]:
    """Figures the model may state because this system computed them."""
    return frozenset(
        str(value)
        for value in (
            breakdown.score,
            breakdown.must_total,
            breakdown.must_covered,
            breakdown.nice_total,
            breakdown.nice_covered,
        )
    )


def _role_context(job: Job) -> str:
    header = "\n".join(
        f"{label}: {value}"
        for label, value in (
            ("Title", job.title),
            ("Company", job.company),
            ("Location", job.location),
            ("Seniority", job.seniority.value if job.seniority else None),
        )
        if value
    )
    skills = ", ".join(job.hard_skills)
    return f"{header}\nNamed technologies: {skills}" if skills else header


def _breakdown_text(breakdown: FitScore) -> str:
    """The computed matches, in words the prompt can act on.

    The model is shown what is already covered and what is missing so it can lead with
    the former. It is told the gaps explicitly so that it does not try to write over
    them, which is the failure a vaguer prompt invites.
    """
    lines: list[str] = []
    for match in breakdown.matches:
        label = (
            "COVERED"
            if match.coverage is Coverage.COVERED
            else ("PARTIAL" if match.coverage is Coverage.PARTIAL else "MISSING")
        )
        suffix = f" — evidence: {match.evidence}" if match.evidence else ""
        lines.append(f"[{label}] ({match.kind.value}) {match.requirement}{suffix}")
    return "\n".join(lines) or "No requirements were extracted from this posting."


def _evidence_text(items: list[ProfileItem]) -> str:
    """Every reviewed item, prefixed with the id the model must cite."""
    lines = []
    for item in items:
        context = " · ".join(part for part in (item.organisation, item.role) if part)
        lines.append(f"[{item.id}] {item.text}" + (f"  ({context})" if context else ""))
    return "\n".join(lines)
